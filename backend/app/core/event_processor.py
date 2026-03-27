"""EventProcessor: routes incoming hook events to focused handler modules.

This module is intentionally kept thin.  All substantive logic lives in the
sub-modules under ``app.core.handlers``.

Public surface (unchanged from before the refactor):
- ``EventProcessor`` class with the same methods and singleton ``event_processor``
- ``derive_git_root`` utility function
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.config import get_settings
from app.core.beads_poller import get_beads_poller, has_beads, init_beads_poller
from app.core.broadcast_service import broadcast_error, broadcast_event, broadcast_state
from app.core.handlers import (
    enrich_agent_from_transcript,
    ensure_task_poller_running,
    handle_agent_update,
    handle_pre_tool_use,
    handle_session_end,
    handle_session_start,
    handle_stop,
    handle_subagent_info,
    handle_subagent_start,
    handle_subagent_stop,
    handle_user_prompt_submit,
    route_teammate_event,
)
from app.core.jsonl_parser import get_last_assistant_response
from app.core.state_machine import StateMachine
from app.core.task_file_poller import init_task_file_poller
from app.core.task_persistence import load_tasks, save_tasks
from app.core.team_registry import TeamRegistry
from app.core.transcript_poller import init_transcript_poller
from app.db.database import AsyncSessionLocal
from app.db.models import EventRecord, SessionRecord
from app.models.agents import AgentState
from app.models.common import TodoItem
from app.models.events import Event, EventData, EventType
from app.models.sessions import ConversationEntry, GameState, HistoryEntry

logger = logging.getLogger(__name__)


def derive_git_root(working_dir: str) -> str | None:
    """Derive the git project root from a working directory.

    Walks up the directory tree looking for a .git directory.
    Returns the path containing .git, or None if not found.

    Args:
        working_dir: Starting directory path

    Returns:
        The git project root path, or None if not a git repository
    """
    if not working_dir:
        return None

    try:
        path = Path(working_dir).resolve()

        for parent in [path, *path.parents]:
            git_dir = parent / ".git"
            if git_dir.exists():
                return str(parent)

            if parent == parent.parent:
                break

        if path.exists() and path.is_dir():
            return str(path)

    except (OSError, ValueError) as e:
        logger.warning(f"Error deriving git root from {working_dir}: {e}")

    return None


class EventProcessor:
    """Routes Claude Code hook events to focused handler modules.

    Maintains the in-memory session registry (``StateMachine`` per session)
    and orchestrates:
    - DB persistence
    - History entry building
    - Task-file and transcript poller lifecycle
    - Delegation to typed handler functions
    - WebSocket broadcasting
    """

    def __init__(self) -> None:
        self.sessions: dict[str, StateMachine] = {}
        self.team_registry = TeamRegistry()
        self._sessions_lock = asyncio.Lock()
        self._transcript_poller_initialized = False
        self._task_poller_initialized = False
        self._beads_poller_initialized = False
        self._beads_sessions: set[str] = set()  # Sessions with active beads polling

    # ------------------------------------------------------------------
    # Poller lifecycle helpers
    # ------------------------------------------------------------------

    def _ensure_transcript_poller(self) -> None:
        """Initialise the transcript poller if not already done."""
        if not self._transcript_poller_initialized:
            init_transcript_poller(self._handle_polled_event)
            self._transcript_poller_initialized = True

    def _ensure_task_file_poller(self) -> None:
        """Initialise the task file poller if not already done."""
        if not self._task_poller_initialized:
            init_task_file_poller(self._handle_task_file_update)
            self._task_poller_initialized = True

    def _ensure_beads_poller(self) -> None:
        """Initialise the beads poller if not already done."""
        if not self._beads_poller_initialized:
            init_beads_poller(self._handle_beads_update)
            self._beads_poller_initialized = True

    # ------------------------------------------------------------------
    # Callbacks for pollers
    # ------------------------------------------------------------------

    async def _handle_task_file_update(self, session_id: str, todos: list[TodoItem]) -> None:
        """Handle task-file updates: update SM, persist to DB, broadcast."""
        sm = self.sessions.get(session_id)
        if not sm:
            return

        sm.todos = todos
        logger.debug(f"Updated todos for session {session_id}: {len(todos)} items")

        await save_tasks(session_id, todos)
        await broadcast_state(session_id, sm)

    async def _handle_beads_update(self, session_id: str, todos: list[TodoItem]) -> None:
        """Handle beads issue updates: update SM and broadcast."""
        sm = self.sessions.get(session_id)
        if not sm:
            return

        sm.todos = todos
        logger.debug(f"Updated beads todos for session {session_id}: {len(todos)} items")

        await save_tasks(session_id, todos)
        await broadcast_state(session_id, sm)

    async def _handle_polled_event(self, event: Event) -> None:
        """Handle events extracted from polled subagent transcripts."""
        logger.debug(
            f"Polled event: {event.event_type} agent={event.data.agent_id} "
            f"tool={event.data.tool_name}"
        )
        await self._process_event_internal(event)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def remove_session(self, session_id: str) -> None:
        """Remove a session's in-memory state.

        Args:
            session_id: Identifier for the session to purge.
        """
        async with self._sessions_lock:
            self.sessions.pop(session_id, None)

    async def clear_all_sessions(self) -> None:
        """Clear all in-memory session state."""
        async with self._sessions_lock:
            self.sessions.clear()

    async def get_current_state(self, session_id: str) -> GameState | None:
        """Retrieve current game state for a session, restoring from DB if needed."""
        if session_id not in self.sessions:
            await self._restore_session(session_id)

        sm = self.sessions.get(session_id)
        if sm:
            return sm.to_game_state(session_id)
        return None

    async def get_project_root(self, session_id: str) -> str | None:
        """Get the cached project_root for a session from the database.

        Args:
            session_id: The session identifier

        Returns:
            The project root path if cached, None otherwise
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionRecord.project_root).where(SessionRecord.id == session_id)
            )
            row = result.scalar_one_or_none()
            return row

    # ------------------------------------------------------------------
    # Public event ingestion
    # ------------------------------------------------------------------

    async def process_event(self, event: Event) -> None:
        """Process an incoming event and update session state."""
        logger.info(
            f"Processing event: {event.event_type} "
            f"Session: {event.session_id} "
            f"Agent: {event.data.agent_id if event.data else 'N/A'}"
        )

        try:
            await self._process_event_internal(event)
        except Exception as e:
            logger.exception(f"Error processing event {event.event_type}: {e}")
            with contextlib.suppress(Exception):
                await broadcast_error(
                    event.session_id,
                    f"Error processing {event.event_type}: {e!s}",
                    event.timestamp.isoformat(),
                )

    # ------------------------------------------------------------------
    # Internal routing
    # ------------------------------------------------------------------

    async def _process_event_internal(self, event: Event) -> None:
        """Persist, update state machine, build history entry, delegate to handlers."""

        # ------------------------------------------------------------------
        # Agent Teams routing: correlate teammate events to lead's session.
        # This runs BEFORE persist so teammate events are stored under the
        # lead's session_id, not their own (avoiding phantom sessions).
        # ------------------------------------------------------------------
        team_name = event.data.team_name if event.data else None
        teammate_name = event.data.teammate_name if event.data else None

        # ------------------------------------------------------------------
        # Late discovery: TeammateIdle/TaskCompleted reveal which session is
        # a teammate. Register the mapping so future events from this
        # session_id get routed to the lead.
        # ------------------------------------------------------------------
        if (
            event.event_type in (EventType.TEAMMATE_IDLE, EventType.TASK_COMPLETED)
            and team_name
            and teammate_name
            and not self.team_registry.is_teammate_session(event.session_id)
        ):
            self.team_registry.register_teammate(team_name, teammate_name, event.session_id)
            # Find the lead: the oldest session with the same project that
            # isn't a registered teammate.
            if not self.team_registry.get_lead_session(team_name):
                for sid in self.sessions:
                    if sid == event.session_id:
                        continue
                    if not self.team_registry.is_teammate_session(sid):
                        # Check if same project by looking at DB
                        project_root = await self.get_project_root(sid)
                        event_project_root = await self.get_project_root(event.session_id)
                        if (
                            project_root
                            and event_project_root
                            and project_root == event_project_root
                        ):
                            self.team_registry.register_lead(team_name, sid)
                            logger.info(
                                f"Late-discovered team lead: team={team_name} session={sid}"
                            )
                            break
            # Create the teammate agent in the lead's office
            lead_session_id = self.team_registry.get_lead_session(team_name)
            if lead_session_id and lead_session_id in self.sessions:
                lead_sm = self.sessions[lead_session_id]
                agent_id = self.team_registry.get_agent_id(team_name, teammate_name)
                if agent_id and agent_id not in lead_sm.agents:
                    synthetic_start = Event(
                        event_type=EventType.SUBAGENT_START,
                        session_id=lead_session_id,
                        timestamp=event.timestamp,
                        data=EventData(
                            agent_id=agent_id,
                            agent_name=teammate_name,
                            task_description=f"Team member: {teammate_name}",
                            team_name=team_name,
                            teammate_name=teammate_name,
                            is_teammate=True,
                        ),
                    )
                    lead_sm.transition(synthetic_start)
                    lead_sm.teammate_agents.add(agent_id)
                    if agent_id in lead_sm.agents:
                        lead_sm.agents[agent_id].state = AgentState.WORKING
                    if agent_id in lead_sm.arrival_queue:
                        lead_sm.arrival_queue.remove(agent_id)
                    await broadcast_state(lead_session_id, lead_sm)
                    logger.info(
                        f"Late-created teammate agent: {agent_id} in lead session {lead_session_id}"
                    )

        # ------------------------------------------------------------------
        # Inject team context for sessions previously identified as teammates
        # (regular hooks like PreToolUse don't include team_name/teammate_name)
        # ------------------------------------------------------------------
        if not team_name and self.team_registry.is_teammate_session(event.session_id):
            team_name = self.team_registry.get_team_name_by_session(event.session_id)
            teammate_name = self.team_registry.get_teammate_name_by_session(event.session_id)
            if event.data and team_name and teammate_name:
                event.data.team_name = team_name
                event.data.teammate_name = teammate_name

        # Try to match new sessions to pre-registered pending teammates
        if not team_name and event.event_type == EventType.SESSION_START:
            match = self.team_registry.try_match_pending_teammate(event.session_id)
            if match:
                team_name, teammate_name = match
                if event.data:
                    event.data.team_name = team_name
                    event.data.teammate_name = teammate_name

        # ------------------------------------------------------------------
        # Lead spawning a teammate: SUBAGENT_START from lead with team_name.
        # Register the lead and pre-register the expected teammate.
        # ------------------------------------------------------------------
        if (
            team_name
            and teammate_name
            and event.event_type == EventType.SUBAGENT_START
            and not self.team_registry.is_teammate_session(event.session_id)
        ):
            # This is the lead's session spawning a teammate
            if not self.team_registry.get_lead_session(team_name):
                self.team_registry.register_lead(team_name, event.session_id)
            # Pre-register teammate (no session_id yet — will be filled on SessionStart)
            self.team_registry.register_teammate(
                team_name, teammate_name, f"pending_{teammate_name}"
            )
            logger.info(f"Lead spawning teammate: {teammate_name} in team {team_name}")
            # Don't process this as a normal subagent — it's a teammate spawn
            # The teammate will appear when their SessionStart arrives
            return

        if team_name and teammate_name:
            # This is a teammate event — register and route
            if event.event_type == EventType.SESSION_START:
                self.team_registry.register_teammate(team_name, teammate_name, event.session_id)

            lead_session_id = self.team_registry.get_lead_session(team_name)
            if not lead_session_id:
                # Lead hasn't started yet — queue for later (don't persist yet)
                self.team_registry.queue_pending_event(team_name, event)
                return

            # Ensure lead's StateMachine exists
            if lead_session_id not in self.sessions:
                await self._restore_session(lead_session_id)
            if lead_session_id not in self.sessions:
                self.sessions[lead_session_id] = StateMachine()

            sm = self.sessions[lead_session_id]
            rewritten = route_teammate_event(event, self.team_registry, sm)
            if rewritten:
                # Mark teammate agents
                if rewritten.event_type == EventType.SUBAGENT_START and rewritten.data.is_teammate:
                    sm.teammate_agents.add(rewritten.data.agent_id or "")
                event = rewritten
            else:
                # Event handled internally (desk subagent) or needs queuing
                await broadcast_state(lead_session_id, sm)
                return

        elif team_name and not teammate_name:
            # This is the lead — register and flush pending events
            if event.event_type == EventType.SESSION_START:
                self.team_registry.register_lead(team_name, event.session_id)

                # Persist the lead event first so the session exists for flushed events
                await self._persist_event(event)

                # Flush any queued teammate events
                pending = self.team_registry.flush_pending_events(team_name)
                for pending_event in pending:
                    await self._process_event_internal(pending_event)

                # Lead SESSION_START already persisted above — skip duplicate
                # Fall through to normal processing (session restore, transition, etc.)

            # Lead's SendMessage → generate TEAMMATE_MESSAGE
            if (
                event.event_type == EventType.PRE_TOOL_USE
                and event.data
                and event.data.tool_name == "SendMessage"
            ):
                tool_input = event.data.tool_input or {}
                recipient = tool_input.get("to", "")
                message_text = str(
                    tool_input.get("message")
                    or tool_input.get("content")
                    or tool_input.get("text")
                    or tool_input.get("prompt", "")
                )

                # Ensure lead SM exists for name resolution
                if event.session_id not in self.sessions:
                    await self._restore_session(event.session_id)
                if event.session_id not in self.sessions:
                    self.sessions[event.session_id] = StateMachine()

                synthetic_msg = Event(
                    event_type=EventType.TEAMMATE_MESSAGE,
                    session_id=event.session_id,
                    timestamp=event.timestamp,
                    data=EventData(
                        agent_id="main",
                        # Don't set teammate_name — that would cause the team
                        # routing to treat this as a teammate event. The lead's
                        # messages go through normal processing.
                        team_name=team_name,
                        message_to=recipient,
                        message_text=message_text,
                    ),
                )
                await self._process_event_internal(synthetic_msg)
                # Don't process the original PRE_TOOL_USE — it would overwrite
                # the boss bubble with "SendMessage..." via _tool_to_thought.
                return

        # Persist the (potentially rewritten) event under the correct session_id.
        # Skip if already persisted (lead SESSION_START with team_name).
        if not (team_name and not teammate_name and event.event_type == EventType.SESSION_START):
            await self._persist_event(event)

        if event.session_id not in self.sessions:
            await self._restore_session(event.session_id)

        if event.session_id not in self.sessions:
            self.sessions[event.session_id] = StateMachine()

        sm = self.sessions[event.session_id]

        sm.transition(event)

        agent_id = event.data.agent_id if event.data and event.data.agent_id else "main"

        # Build detail dict from event data fields for frontend inspection.
        detail: dict[str, Any] = {}
        if event.data:
            for src, dst in [
                ("tool_name", "toolName"),
                ("tool_input", "toolInput"),
                ("result_summary", "resultSummary"),
                ("message", "message"),
                ("thinking", "thinking"),
                ("error_type", "errorType"),
                ("task_description", "taskDescription"),
                ("agent_name", "agentName"),
                ("prompt", "prompt"),
                ("teammate_name", "teammateName"),
                ("message_to", "messageTo"),
                ("message_text", "messageText"),
            ]:
                val = getattr(event.data, src, None)
                if val is not None:
                    detail[dst] = val

        # For TEAMMATE_MESSAGE, resolve recipientId from the state machine
        if event.event_type == EventType.TEAMMATE_MESSAGE and event.data and event.data.message_to:
            recipient_id_for_detail = sm.find_agent_by_teammate_name(event.data.message_to)
            if recipient_id_for_detail:
                detail["recipientId"] = recipient_id_for_detail
            # Also include sender's display name and color
            sender = sm.agents.get(agent_id)
            if sender:
                detail["agentName"] = sender.name or agent_id
                detail["agentColor"] = sender.color

        event_dict: HistoryEntry = {
            "id": str(event.timestamp.timestamp()),
            "type": str(event.event_type),
            "agentId": agent_id,
            "summary": self._get_event_summary(event),
            "timestamp": event.timestamp.isoformat(),
            "detail": detail,
        }
        sm.history.append(event_dict)
        if len(sm.history) > 500:
            sm.history = sm.history[-500:]

        # ------------------------------------------------------------------
        # SESSION_START – start task-file polling + beads polling
        # ------------------------------------------------------------------
        if event.event_type == EventType.SESSION_START:
            await handle_session_start(sm, event, self._ensure_task_file_poller)
            await self._start_beads_if_available(event.session_id)

        # ------------------------------------------------------------------
        # Auto-start task polling for missed SESSION_START (backend restart)
        # ------------------------------------------------------------------
        await ensure_task_poller_running(
            sm,
            event,
            self._ensure_task_file_poller,
            self._derive_task_list_id,
        )
        await self._start_beads_if_available(event.session_id)

        # ------------------------------------------------------------------
        # SESSION_END – stop task-file polling + beads polling
        # ------------------------------------------------------------------
        if event.event_type == EventType.SESSION_END:
            await handle_session_end(sm, event)
            beads = get_beads_poller()
            if beads:
                await beads.stop_polling(event.session_id)
            self._beads_sessions.discard(event.session_id)

        # ------------------------------------------------------------------
        # Default state broadcast + history event notification
        # ------------------------------------------------------------------
        await broadcast_state(event.session_id, sm)
        await broadcast_event(event.session_id, event_dict)

        # ------------------------------------------------------------------
        # SUBAGENT_START
        # ------------------------------------------------------------------
        if event.event_type == EventType.SUBAGENT_START:
            await handle_subagent_start(
                sm,
                event,
                self._ensure_transcript_poller,
                self._update_agent_state,
            )

        # ------------------------------------------------------------------
        # SUBAGENT_INFO
        # ------------------------------------------------------------------
        if event.event_type == EventType.SUBAGENT_INFO:
            await handle_subagent_info(sm, event, self._ensure_transcript_poller)

        # ------------------------------------------------------------------
        # AGENT_UPDATE
        # ------------------------------------------------------------------
        if event.event_type == EventType.AGENT_UPDATE:
            await handle_agent_update(sm, event)

        # ------------------------------------------------------------------
        # SUBAGENT_STOP
        # ------------------------------------------------------------------
        if event.event_type == EventType.SUBAGENT_STOP:
            await handle_subagent_stop(sm, event, self._persist_synthetic_event)

        # ------------------------------------------------------------------
        # STOP
        # ------------------------------------------------------------------
        if event.event_type == EventType.STOP:
            await handle_stop(sm, event, agent_id)

        # ------------------------------------------------------------------
        # USER_PROMPT_SUBMIT
        # ------------------------------------------------------------------
        if event.event_type == EventType.USER_PROMPT_SUBMIT:
            await handle_user_prompt_submit(sm, event, agent_id)

        # ------------------------------------------------------------------
        # TEAMMATE_MESSAGE — add to conversation
        # ------------------------------------------------------------------
        if event.event_type == EventType.TEAMMATE_MESSAGE and event.data:
            sender_agent_id = event.data.agent_id or "unknown"
            sender_agent = sm.agents.get(sender_agent_id)
            recipient_name = event.data.message_to or "unknown"
            recipient_id = sm.find_agent_by_teammate_name(recipient_name)

            sender_name = (
                "Team Lead"
                if sender_agent_id == "main"
                else (sender_agent.name or "unknown")
                if sender_agent
                else (event.data.teammate_name or "unknown")
            )
            sender_color = sender_agent.color if sender_agent else "#f97316"
            # Resolve recipient display name from their agent (consistent with desk label)
            recipient_agent = sm.agents.get(recipient_id) if recipient_id else None
            resolved_recipient_name = (
                (recipient_agent.name or recipient_name) if recipient_agent else recipient_name
            )
            msg_entry = ConversationEntry(
                id=str(event.timestamp.timestamp()),
                role="team_message",
                agentId=sender_agent_id,
                text=event.data.message_text or "",
                timestamp=event.timestamp.isoformat(),
                agentName=sender_name,
                agentColor=sender_color,
                recipientId=recipient_id or "",
                recipientName=resolved_recipient_name,
            )
            sm.conversation.append(msg_entry)

        # ------------------------------------------------------------------
        # PRE_TOOL_USE
        # ------------------------------------------------------------------
        if event.event_type == EventType.PRE_TOOL_USE:
            await handle_pre_tool_use(sm, event, agent_id, self._get_event_summary(event))

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _persist_synthetic_event(
        self, session_id: str, event_type: EventType, data: EventData | dict[str, Any] | None
    ) -> None:
        """Save an intermediate lifecycle state to the DB for perfect replay."""
        payload: dict[str, Any]
        if data is None:
            payload = {}
        elif isinstance(data, EventData):
            payload = data.model_dump()
        else:
            payload = data
        async with AsyncSessionLocal() as db:
            event_rec = EventRecord(
                session_id=session_id,
                timestamp=datetime.now(UTC),
                event_type=event_type.value,
                data=payload,
            )
            db.add(event_rec)
            await db.commit()

    async def _restore_session(self, session_id: str) -> None:
        """Reconstruct a StateMachine from DB events."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(EventRecord)
                .where(EventRecord.session_id == session_id)
                .order_by(EventRecord.timestamp.asc())
            )
            events = result.scalars().all()

            if not events:
                return

            logger.info(f"Restoring session {session_id} from {len(events)} events in DB")

            sm = StateMachine()
            skipped_count = 0
            for rec in events:
                try:
                    evt = Event(
                        event_type=EventType(rec.event_type),
                        session_id=rec.session_id,
                        timestamp=rec.timestamp,
                        data=EventData.model_validate(rec.data) if rec.data else EventData(),
                    )
                    sm.transition(evt)

                    agent_id = evt.data.agent_id if evt.data and evt.data.agent_id else "main"
                    history_entry: HistoryEntry = {
                        "id": str(evt.timestamp.timestamp()),
                        "type": str(evt.event_type),
                        "agentId": agent_id,
                        "summary": self._get_event_summary(evt),
                        "timestamp": evt.timestamp.isoformat(),
                        "detail": {},
                    }
                    sm.history.append(history_entry)

                    # Rebuild conversation from stored events.
                    if (
                        evt.event_type == EventType.USER_PROMPT_SUBMIT
                        and evt.data
                        and evt.data.prompt
                        and "<task-notification>" not in evt.data.prompt
                    ):
                        conv_entry: ConversationEntry = {
                            "id": str(evt.timestamp.timestamp()),
                            "role": "user",
                            "agentId": agent_id,
                            "text": evt.data.prompt,
                            "timestamp": evt.timestamp.isoformat(),
                        }
                        sm.conversation.append(conv_entry)
                    elif evt.event_type == EventType.PRE_TOOL_USE and evt.data:
                        if evt.data.thinking:
                            thinking_entry: ConversationEntry = {
                                "id": f"{evt.timestamp.timestamp()}_thinking",
                                "role": "thinking",
                                "agentId": agent_id,
                                "text": evt.data.thinking,
                                "timestamp": evt.timestamp.isoformat(),
                            }
                            sm.conversation.append(thinking_entry)
                        if evt.data.tool_name:
                            tool_entry: ConversationEntry = {
                                "id": f"{evt.timestamp.timestamp()}_tool",
                                "role": "tool",
                                "agentId": agent_id,
                                "text": self._get_event_summary(evt),
                                "timestamp": evt.timestamp.isoformat(),
                                "toolName": evt.data.tool_name,
                            }
                            sm.conversation.append(tool_entry)
                    elif evt.event_type == EventType.STOP and evt.data and evt.data.transcript_path:
                        settings = get_settings()
                        translated_path = settings.translate_path(evt.data.transcript_path)
                        response = get_last_assistant_response(translated_path)
                        if response:
                            assistant_entry: ConversationEntry = {
                                "id": str(evt.timestamp.timestamp()),
                                "role": "assistant",
                                "agentId": agent_id,
                                "text": response,
                                "timestamp": evt.timestamp.isoformat(),
                            }
                            sm.conversation.append(assistant_entry)
                    elif (
                        evt.event_type == EventType.SUBAGENT_INFO
                        and evt.data
                        and evt.data.agent_transcript_path
                    ):
                        native_agent_id = evt.data.native_agent_id
                        transcript_path = evt.data.agent_transcript_path
                        for agent in sm.agents.values():
                            if agent.native_id == native_agent_id or agent.native_id is None:
                                if native_agent_id and agent.native_id is None:
                                    agent.native_id = native_agent_id
                                if (
                                    not agent.current_task
                                    or agent.current_task == "Resumed mid-session"
                                ):
                                    await enrich_agent_from_transcript(
                                        agent, transcript_path, evt.data.agent_type
                                    )
                                break
                except Exception as e:
                    skipped_count += 1
                    logger.warning(
                        f"Skipping malformed event {rec.id} (type={rec.event_type}): {e}"
                    )
                    continue

            if skipped_count > 0:
                logger.warning(f"Skipped {skipped_count} malformed events during restoration")

            if len(sm.history) > 500:
                sm.history = sm.history[-500:]

            sm.todos = await load_tasks(session_id)
            logger.debug(f"Restored {len(sm.todos)} tasks for session {session_id}")

            self.sessions[session_id] = sm

    async def _persist_event(self, event: Event) -> None:
        """Save event to database and manage session records."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionRecord).where(SessionRecord.id == event.session_id)
            )
            session_rec = result.scalar_one_or_none()

            project_name = event.data.project_name if event.data else None
            project_dir = event.data.project_dir if event.data else None
            working_dir = event.data.working_dir if event.data else None

            source_dir = project_dir or working_dir
            project_root = derive_git_root(source_dir) if source_dir else None

            if not session_rec:
                session_rec = SessionRecord(
                    id=event.session_id,
                    project_name=project_name,
                    project_root=project_root,
                )
                db.add(session_rec)
            else:
                if project_name and not session_rec.project_name:
                    session_rec.project_name = project_name

                if project_root and not session_rec.project_root:
                    session_rec.project_root = project_root
                    logger.info(
                        f"Cached project_root for session {event.session_id}: {project_root}"
                    )

                if event.event_type == EventType.SESSION_START:
                    await db.execute(
                        delete(EventRecord).where(EventRecord.session_id == event.session_id)
                    )
                    session_rec.status = "active"
                    session_rec.updated_at = datetime.now(UTC)
                    if project_name:
                        session_rec.project_name = project_name
                    if project_root:
                        session_rec.project_root = project_root

            if event.event_type == EventType.SESSION_END:
                session_rec.status = "completed"
                session_rec.updated_at = datetime.now(UTC)

            event_rec = EventRecord(
                session_id=event.session_id,
                timestamp=event.timestamp,
                event_type=event.event_type,
                data=event.data.model_dump() if event.data else {},
            )
            db.add(event_rec)
            await db.commit()

    # ------------------------------------------------------------------
    # State update helpers
    # ------------------------------------------------------------------

    async def _update_agent_state(self, session_id: str, agent_id: str, state: AgentState) -> None:
        """Update an agent's state and broadcast to clients."""
        sm = self.sessions.get(session_id)
        if sm and agent_id in sm.agents:
            sm.agents[agent_id].state = state
            if state in [
                AgentState.WALKING_TO_DESK,
                AgentState.LEAVING,
                AgentState.COMPLETED,
                AgentState.WAITING,
            ]:
                sm.agents[agent_id].bubble = None
            # Remove from arrival queue when agent moves to desk
            if state == AgentState.WALKING_TO_DESK and agent_id in sm.arrival_queue:
                sm.arrival_queue.remove(agent_id)
            await broadcast_state(session_id, sm)

    async def _start_beads_if_available(self, session_id: str) -> None:
        """Start beads polling if the session's project has a .beads/ directory."""
        if session_id in self._beads_sessions:
            return
        project_root = await self.get_project_root(session_id)
        if not project_root:
            return
        if has_beads(project_root):
            self._ensure_beads_poller()
            beads = get_beads_poller()
            if beads and not await beads.is_polling(session_id):
                await beads.start_polling(session_id, project_root)
                self._beads_sessions.add(session_id)
        else:
            # Only log once per session to avoid spam
            self._beads_sessions.add(session_id)

    async def _derive_task_list_id(self, session_id: str) -> str | None:
        """Derive the task_list_id from the session's project root.

        Args:
            session_id: The session identifier.

        Returns:
            Named task folder identifier, or None.
        """
        from app.core.handlers.session_handler import (
            derive_task_list_id_from_root,
        )

        project_root = await self.get_project_root(session_id)
        result = derive_task_list_id_from_root(project_root)
        if result:
            logger.debug(f"Derived task_list_id '{result}' for session {session_id}")
        return result

    # ------------------------------------------------------------------
    # Event summary (used by replay endpoint and history building)
    # ------------------------------------------------------------------

    def get_event_summary(self, event: Event) -> str:
        """Public wrapper for generating event summaries."""
        return self._get_event_summary(event)

    def _get_event_summary(self, event: Event) -> str:
        """Generate a human-readable summary for the event log."""
        if not event.data:
            return f"{event.event_type} event received"

        data = event.data
        match event.event_type:
            case EventType.SESSION_START:
                return "Claude Office session started"
            case EventType.SESSION_END:
                return "Claude Office session ended"
            case EventType.PRE_TOOL_USE:
                tool = data.tool_name or "Unknown tool"
                target = ""
                if data.tool_input:
                    target = (
                        data.tool_input.get("file_path") or data.tool_input.get("command") or ""
                    )
                    if len(target) > 30:
                        target = f"...{target[-27:]}"
                return f"Using {tool} {target}".strip()
            case EventType.POST_TOOL_USE:
                return f"Completed {data.tool_name or 'tool'}"
            case EventType.USER_PROMPT_SUBMIT:
                prompt = data.prompt or ""
                if len(prompt) > 40:
                    prompt = f"{prompt[:37]}..."
                return f"User: {prompt}" if prompt else "User submitted prompt"
            case EventType.PERMISSION_REQUEST:
                tool = data.tool_name or "tool"
                return f"Waiting for permission: {tool}"
            case EventType.SUBAGENT_START:
                return f"Spawned subagent: {data.agent_name or data.agent_id}"
            case EventType.SUBAGENT_STOP:
                status = "successfully" if data.success else "with errors"
                return f"Subagent {data.agent_id} finished {status}"
            case EventType.STOP:
                return "Main agent task complete"
            case EventType.CLEANUP:
                return f"Agent {data.agent_id} left the building"
            case EventType.NOTIFICATION:
                return f"Notification: {data.message or data.notification_type or 'info'}"
            case EventType.REPORTING:
                return f"Agent {data.agent_id or 'unknown'} reporting"
            case EventType.WALKING_TO_DESK:
                return f"Agent {data.agent_id or 'unknown'} walking to desk"
            case EventType.WAITING:
                return f"Agent {data.agent_id or 'unknown'} waiting in queue"
            case EventType.LEAVING:
                return f"Agent {data.agent_id or 'unknown'} leaving"
            case EventType.ERROR:
                return f"Error: {data.message or 'unknown error'}"
            case EventType.BACKGROUND_TASK_NOTIFICATION:
                task_id = data.background_task_id or "unknown"
                status = data.background_task_status or "completed"
                summary = data.background_task_summary or ""
                task_id_short = task_id[:7] if len(task_id) > 7 else task_id
                summary_short = (summary[:40] + "...") if len(summary) > 40 else summary
                return f"Background task {task_id_short} {status}: {summary_short}"
            case EventType.TEAMMATE_MESSAGE:
                sender = data.teammate_name or "Unknown"
                recipient = data.message_to or "Unknown"
                snippet = (data.message_text or "")[:30]
                if len(data.message_text or "") > 30:
                    snippet += "..."
                return f"{sender} → {recipient}: {snippet}"
            case EventType.TEAMMATE_IDLE:
                return f"{data.teammate_name or 'Teammate'} finished their turn"
            case EventType.TASK_COMPLETED:
                subject = data.task_subject or "a task"
                name = data.teammate_name or "Someone"
                return f"{name} completed: {subject}"
            case _:
                return f"Event: {event.event_type}"


event_processor = EventProcessor()
