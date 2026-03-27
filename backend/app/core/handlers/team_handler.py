"""Handler for Agent Teams event routing.

Routes teammate session events into the lead's StateMachine by rewriting
event types and session IDs.
"""

import logging
from typing import Any

from app.core.state_machine import StateMachine
from app.core.team_registry import TeamRegistry
from app.models.agents import DeskSubagent
from app.models.events import Event, EventData, EventType

logger = logging.getLogger(__name__)


def route_teammate_event(
    event: Event,
    registry: TeamRegistry,
    sm: StateMachine,
) -> Event | None:
    """Rewrite a teammate event to target the lead's session.

    Returns the rewritten Event, or None if the lead session is not yet
    registered (caller should queue the event), or if the event was
    handled internally (e.g., desk subagent management).

    Args:
        event: The incoming teammate event.
        registry: The team registry for session lookups.
        sm: The lead's StateMachine (used for subagent context).

    Returns:
        Rewritten Event targeting the lead's session, or None.
    """
    if not event.data or not event.data.team_name or not event.data.teammate_name:
        return None

    team_name = event.data.team_name
    teammate_name = event.data.teammate_name
    lead_session_id = registry.get_lead_session(team_name)

    if not lead_session_id:
        return None

    agent_id = registry.get_agent_id(team_name, teammate_name)
    if not agent_id:
        agent_id = registry.register_teammate(team_name, teammate_name, event.session_id)

    # --- Teammate's own subagents → desk subagents ---
    if event.event_type == EventType.SUBAGENT_START and agent_id in sm.agents:
        agent = sm.agents[agent_id]
        if agent.is_teammate:
            sub_id = event.data.agent_id or "unknown_sub"
            sub_name = event.data.agent_name
            agent.desk_subagents.append(DeskSubagent(id=sub_id, name=sub_name, state="working"))
            logger.info(f"Added desk subagent {sub_id} to teammate {agent_id}")
            return None  # Handled internally, no event to forward

    if event.event_type == EventType.SUBAGENT_STOP and agent_id in sm.agents:
        agent = sm.agents[agent_id]
        if agent.is_teammate:
            sub_id = event.data.agent_id
            if sub_id:
                agent.desk_subagents = [s for s in agent.desk_subagents if s.id != sub_id]
                logger.info(f"Removed desk subagent {sub_id} from teammate {agent_id}")
            return None  # Handled internally

    # --- SendMessage tool detection ---
    if event.event_type == EventType.PRE_TOOL_USE and event.data.tool_name == "SendMessage":
        tool_input: dict[str, Any] = event.data.tool_input or {}
        recipient = tool_input.get("to", "")
        message_text = (
            tool_input.get("message")
            or tool_input.get("content")
            or tool_input.get("text")
            or tool_input.get("prompt", "")
        )

        return Event(
            event_type=EventType.TEAMMATE_MESSAGE,
            session_id=lead_session_id,
            timestamp=event.timestamp,
            data=EventData(
                agent_id=agent_id,
                team_name=team_name,
                teammate_name=teammate_name,
                message_to=recipient,
                message_text=str(message_text),
            ),
        )

    # --- Event type rewriting ---
    if event.event_type == EventType.SESSION_START:
        return Event(
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

    elif event.event_type == EventType.SESSION_END:
        return Event(
            event_type=EventType.SUBAGENT_STOP,
            session_id=lead_session_id,
            timestamp=event.timestamp,
            data=EventData(
                agent_id=agent_id,
                team_name=team_name,
                teammate_name=teammate_name,
            ),
        )

    elif event.event_type == EventType.STOP:
        # Teammate finished a turn — idle, don't depart
        return Event(
            event_type=EventType.TEAMMATE_IDLE,
            session_id=lead_session_id,
            timestamp=event.timestamp,
            data=EventData(
                agent_id=agent_id,
                team_name=team_name,
                teammate_name=teammate_name,
            ),
        )

    else:
        # PRE_TOOL_USE, POST_TOOL_USE, etc. — rewrite session + agent_id
        event.data.agent_id = agent_id
        event.session_id = lead_session_id
        return event
