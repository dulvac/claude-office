"""Tests for team event routing handler."""

from datetime import datetime

from app.core.handlers.team_handler import route_teammate_event
from app.core.state_machine import StateMachine
from app.core.team_registry import TeamRegistry
from app.models.agents import Agent, AgentState, DeskSubagent
from app.models.events import Event, EventData, EventType


def make_event(event_type: EventType, session_id: str, **data_kwargs: object) -> Event:
    return Event(
        event_type=event_type,
        session_id=session_id,
        timestamp=datetime.now(),
        data=EventData(**data_kwargs),  # type: ignore[arg-type]
    )


class TestRouteTeammateEvent:
    """Tests for route_teammate_event logic."""

    def test_teammate_session_start_creates_agent(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        event = make_event(
            EventType.SESSION_START,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
        )

        result = route_teammate_event(event, registry, sm)

        assert result is not None
        assert result.event_type == EventType.SUBAGENT_START
        assert result.session_id == "sess_lead"
        assert result.data.agent_id == "teammate_researcher"
        assert result.data.agent_name == "researcher"
        assert result.data.is_teammate is True

    def test_teammate_session_end_creates_subagent_stop(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        event = make_event(
            EventType.SESSION_END,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
        )

        result = route_teammate_event(event, registry, sm)

        assert result is not None
        assert result.event_type == EventType.SUBAGENT_STOP
        assert result.session_id == "sess_lead"
        assert result.data.agent_id == "teammate_researcher"

    def test_teammate_tool_use_rewrites_session_and_agent(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
            tool_name="Read",
            tool_use_id="tu_123",
        )

        result = route_teammate_event(event, registry, sm)

        assert result is not None
        assert result.session_id == "sess_lead"
        assert result.data.agent_id == "teammate_researcher"
        assert result.data.tool_name == "Read"

    def test_teammate_stop_becomes_idle_not_departure(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        event = make_event(
            EventType.STOP,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
        )

        result = route_teammate_event(event, registry, sm)

        assert result is not None
        assert result.event_type == EventType.TEAMMATE_IDLE
        assert result.session_id == "sess_lead"

    def test_returns_none_when_lead_not_registered(self) -> None:
        registry = TeamRegistry()
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
            tool_name="Read",
        )

        result = route_teammate_event(event, registry, sm)
        assert result is None

    def test_send_message_generates_teammate_message(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")
        registry.register_teammate("my-team", "architect", "sess_architect")

        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
            tool_name="SendMessage",
            tool_input={"to": "architect", "text": "Found 3 bugs in auth"},
        )

        result = route_teammate_event(event, registry, sm)

        assert result is not None
        assert result.event_type == EventType.TEAMMATE_MESSAGE
        assert result.data.agent_id == "teammate_researcher"
        assert result.data.message_to == "architect"
        assert result.data.message_text == "Found 3 bugs in auth"


class TestDeskSubagentRouting:
    """Tests for teammate subagent → desk subagent routing."""

    def test_teammate_subagent_start_adds_desk_subagent(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        sm.agents["teammate_researcher"] = Agent(
            id="teammate_researcher",
            name="researcher",
            color="#3B82F6",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
        )
        sm.teammate_agents.add("teammate_researcher")

        event = make_event(
            EventType.SUBAGENT_START,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
            agent_id="subagent_xyz",
            agent_name="helper",
            task_description="Search for bugs",
        )

        result = route_teammate_event(event, registry, sm)

        assert result is None  # Handled internally
        assert len(sm.agents["teammate_researcher"].desk_subagents) == 1
        assert sm.agents["teammate_researcher"].desk_subagents[0].id == "subagent_xyz"
        assert sm.agents["teammate_researcher"].desk_subagents[0].state == "working"

    def test_teammate_subagent_stop_removes_desk_subagent(self) -> None:
        registry = TeamRegistry()
        registry.register_lead("my-team", "sess_lead")
        registry.register_teammate("my-team", "researcher", "sess_researcher")

        sm = StateMachine()
        sm.agents["teammate_researcher"] = Agent(
            id="teammate_researcher",
            name="researcher",
            color="#3B82F6",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
            desk_subagents=[
                DeskSubagent(id="subagent_xyz", name="helper", state="working"),
            ],
        )
        sm.teammate_agents.add("teammate_researcher")

        event = make_event(
            EventType.SUBAGENT_STOP,
            session_id="sess_researcher",
            team_name="my-team",
            teammate_name="researcher",
            agent_id="subagent_xyz",
        )

        result = route_teammate_event(event, registry, sm)

        assert result is None  # Handled internally
        assert len(sm.agents["teammate_researcher"].desk_subagents) == 0
