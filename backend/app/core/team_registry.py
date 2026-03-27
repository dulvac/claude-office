"""Team registry for correlating Agent Team sessions.

Maps team membership: which sessions belong to which team, and routes
teammate events into the lead's StateMachine.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from app.models.events import Event

logger = logging.getLogger(__name__)

TEAMS_DIR = Path.home() / ".claude" / "teams"


def scan_team_configs() -> dict[str, list[str]]:
    """Scan ~/.claude/teams/*/config.json and return team_name → member_names."""
    teams: dict[str, list[str]] = {}
    if not TEAMS_DIR.exists():
        return teams
    for config_path in TEAMS_DIR.glob("*/config.json"):
        try:
            config = json.loads(config_path.read_text())
            team_name = config_path.parent.name
            members = config.get("members", [])
            # Exclude the leader — they're the lead session
            teammate_names: list[str] = [
                str(m["name"])
                for m in members
                if isinstance(m, dict) and m.get("agentType") != "leader" and "name" in m
            ]
            if teammate_names:
                teams[team_name] = teammate_names
                logger.info(f"Found team config: {team_name} with members: {teammate_names}")
        except Exception as e:
            logger.debug(f"Error reading team config {config_path}: {e}")
    return teams


@dataclass
class TeamMember:
    """A registered teammate."""

    teammate_name: str
    teammate_session_id: str
    agent_id: str


@dataclass
class TeamInfo:
    """Team state including lead and all members."""

    team_name: str
    lead_session_id: str | None = None
    members: dict[str, TeamMember] = field(
        default_factory=lambda: cast(dict[str, "TeamMember"], {})
    )


class TeamRegistry:
    """In-memory registry of Agent Teams.

    Maps team_name -> TeamInfo, with methods to register leads/teammates,
    look up session mappings, and queue events for teams whose lead
    hasn't started yet.
    """

    def __init__(self) -> None:
        self._teams: dict[str, TeamInfo] = {}
        self._pending_events: dict[str, list[Event]] = {}
        # Reverse lookup: teammate_session_id -> (team_name, teammate_name)
        self._session_to_teammate: dict[str, tuple[str, str]] = {}

    def _ensure_team(self, team_name: str) -> TeamInfo:
        if team_name not in self._teams:
            self._teams[team_name] = TeamInfo(team_name=team_name)
        return self._teams[team_name]

    def register_lead(self, team_name: str, session_id: str) -> None:
        """Register the lead session for a team."""
        team = self._ensure_team(team_name)
        team.lead_session_id = session_id
        logger.info(f"Registered team lead: team={team_name} session={session_id}")

    def register_teammate(self, team_name: str, teammate_name: str, session_id: str) -> str:
        """Register a teammate and return their agent_id for the lead's office."""
        team = self._ensure_team(team_name)
        agent_id = f"teammate_{teammate_name}"

        member = TeamMember(
            teammate_name=teammate_name,
            teammate_session_id=session_id,
            agent_id=agent_id,
        )
        team.members[teammate_name] = member
        self._session_to_teammate[session_id] = (team_name, teammate_name)
        logger.info(
            f"Registered teammate: team={team_name} name={teammate_name} "
            f"session={session_id} agent_id={agent_id}"
        )
        return agent_id

    def get_lead_session(self, team_name: str) -> str | None:
        """Get the lead session ID for a team, or None."""
        team = self._teams.get(team_name)
        return team.lead_session_id if team else None

    def get_agent_id(self, team_name: str, teammate_name: str) -> str | None:
        """Get the agent_id for a teammate in the lead's office."""
        team = self._teams.get(team_name)
        if not team:
            return None
        member = team.members.get(teammate_name)
        return member.agent_id if member else None

    def is_teammate_session(self, session_id: str) -> bool:
        """Check if a session_id belongs to a registered teammate."""
        return session_id in self._session_to_teammate

    def get_teammate_name_by_session(self, session_id: str) -> str | None:
        """Look up teammate name from session_id."""
        entry = self._session_to_teammate.get(session_id)
        return entry[1] if entry else None

    def get_team_name_by_session(self, session_id: str) -> str | None:
        """Look up team name from a teammate's session_id."""
        entry = self._session_to_teammate.get(session_id)
        return entry[0] if entry else None

    def queue_pending_event(self, team_name: str, event: Event) -> None:
        """Queue an event for a team whose lead hasn't registered yet."""
        if team_name not in self._pending_events:
            self._pending_events[team_name] = []
        self._pending_events[team_name].append(event)
        logger.debug(f"Queued pending event for team {team_name}: {event.event_type}")

    def get_pending_events(self, team_name: str) -> list[Event]:
        """Get pending events without flushing."""
        return self._pending_events.get(team_name, [])

    def flush_pending_events(self, team_name: str) -> list[Event]:
        """Flush and return all pending events for a team."""
        events = self._pending_events.pop(team_name, [])
        if events:
            logger.info(f"Flushed {len(events)} pending events for team {team_name}")
        return events

    def get_all_teammates(self, team_name: str) -> list[TeamMember]:
        """Get all registered teammates for a team."""
        team = self._teams.get(team_name)
        if not team:
            return []
        return list(team.members.values())

    def try_match_pending_teammate(self, session_id: str) -> tuple[str, str] | None:
        """Match a new session to a pre-registered pending teammate.

        When the lead spawns a teammate via Agent tool, we pre-register
        with a "pending_X" session_id. When the actual SessionStart
        arrives, this method finds the pending registration and updates it.

        Returns (team_name, teammate_name) or None.
        """
        for team_name, team in self._teams.items():
            for tname, member in team.members.items():
                if member.teammate_session_id.startswith("pending_"):
                    # Found a pending teammate — assign this session to it
                    member.teammate_session_id = session_id
                    self._session_to_teammate[session_id] = (team_name, tname)
                    logger.info(
                        f"Matched pending teammate {tname} to session {session_id} "
                        f"in team {team_name}"
                    )
                    return (team_name, tname)
        return None

    def try_early_detect_teammate(
        self, session_id: str, lead_session_id: str
    ) -> tuple[str, str] | None:
        """Check if a new session is a teammate by scanning team configs.

        Called when a new session starts from the same project as an
        existing session. Scans ~/.claude/teams/ for active configs
        and assigns the next unregistered member name.

        Returns (team_name, teammate_name) or None.
        """
        configs = scan_team_configs()
        for team_name, member_names in configs.items():
            # Register lead if not already done
            if not self.get_lead_session(team_name):
                self.register_lead(team_name, lead_session_id)

            # Find next unassigned member name
            team = self._teams.get(team_name)
            if not team:
                continue
            assigned_names = {m.teammate_name for m in team.members.values()}
            for name in member_names:
                if name not in assigned_names:
                    self.register_teammate(team_name, name, session_id)
                    logger.info(
                        f"Early-detected teammate: {name} (session={session_id}) "
                        f"in team {team_name}"
                    )
                    return (team_name, name)
        return None
