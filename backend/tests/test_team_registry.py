"""Tests for Agent Teams models and registry."""

from datetime import datetime

from app.core.team_registry import TeamRegistry
from app.models.agents import Agent, AgentState, DeskSubagent
from app.models.events import Event, EventData, EventType


class TestTeamEventTypes:
    """Tests for new team-related EventType values."""

    def test_teammate_message_event_type(self) -> None:
        assert EventType.TEAMMATE_MESSAGE == "teammate_message"

    def test_teammate_idle_event_type(self) -> None:
        assert EventType.TEAMMATE_IDLE == "teammate_idle"

    def test_task_completed_event_type(self) -> None:
        assert EventType.TASK_COMPLETED == "task_completed"


class TestTeamEventData:
    """Tests for new EventData team fields."""

    def test_team_fields_default_none(self) -> None:
        data = EventData()
        assert data.team_name is None
        assert data.teammate_name is None
        assert data.message_to is None
        assert data.message_text is None
        assert data.task_subject is None

    def test_team_fields_round_trip(self) -> None:
        data = EventData(
            team_name="my-team",
            teammate_name="researcher",
            message_to="architect",
            message_text="Found a bug in auth",
            task_subject="Audit auth module",
        )
        assert data.team_name == "my-team"
        assert data.teammate_name == "researcher"
        assert data.message_to == "architect"
        assert data.message_text == "Found a bug in auth"
        assert data.task_subject == "Audit auth module"


class TestDeskSubagent:
    """Tests for DeskSubagent model."""

    def test_desk_subagent_creation(self) -> None:
        sub = DeskSubagent(id="sub_1", name="helper", tool_name="Bash", state="working")
        assert sub.id == "sub_1"
        assert sub.name == "helper"
        assert sub.tool_name == "Bash"
        assert sub.state == "working"

    def test_desk_subagent_camel_case_serialization(self) -> None:
        sub = DeskSubagent(id="sub_1", tool_name="Edit", state="completed")
        data = sub.model_dump(by_alias=True)
        assert "toolName" in data
        assert data["toolName"] == "Edit"


class TestAgentTeamFields:
    """Tests for Agent team-related fields."""

    def test_agent_is_teammate_defaults_false(self) -> None:
        agent = Agent(id="a1", color="#fff", number=1, state=AgentState.WORKING)
        assert agent.is_teammate is False
        assert agent.desk_subagents == []

    def test_agent_with_teammate_and_desk_subagents(self) -> None:
        sub = DeskSubagent(id="sub_1", state="working")
        agent = Agent(
            id="a1",
            color="#fff",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
            desk_subagents=[sub],
        )
        assert agent.is_teammate is True
        assert len(agent.desk_subagents) == 1

    def test_agent_team_fields_serialize_camel(self) -> None:
        agent = Agent(id="a1", color="#fff", number=1, state=AgentState.WORKING, is_teammate=True)
        data = agent.model_dump(by_alias=True)
        assert "isTeammate" in data
        assert "deskSubagents" in data


class TestTeamRegistry:
    """Tests for in-memory TeamRegistry."""

    def test_register_lead(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        assert reg.get_lead_session("my-team") == "sess_lead"

    def test_get_lead_session_unknown_team(self) -> None:
        reg = TeamRegistry()
        assert reg.get_lead_session("no-such-team") is None

    def test_register_teammate(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        agent_id = reg.register_teammate("my-team", "researcher", "sess_researcher")
        assert agent_id == "teammate_researcher"
        assert reg.get_agent_id("my-team", "researcher") == "teammate_researcher"

    def test_register_teammate_before_lead_returns_agent_id(self) -> None:
        reg = TeamRegistry()
        agent_id = reg.register_teammate("my-team", "researcher", "sess_researcher")
        assert agent_id == "teammate_researcher"
        assert reg.get_lead_session("my-team") is None

    def test_is_teammate_session(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        reg.register_teammate("my-team", "researcher", "sess_researcher")
        assert reg.is_teammate_session("sess_researcher") is True
        assert reg.is_teammate_session("sess_lead") is False
        assert reg.is_teammate_session("unknown") is False

    def test_queue_and_flush_pending_events(self) -> None:
        reg = TeamRegistry()
        event = Event(
            event_type=EventType.PRE_TOOL_USE,
            session_id="sess_researcher",
            timestamp=datetime.now(),
            data=EventData(team_name="my-team", teammate_name="researcher"),
        )
        reg.queue_pending_event("my-team", event)
        assert len(reg.get_pending_events("my-team")) == 1

        flushed = reg.flush_pending_events("my-team")
        assert len(flushed) == 1
        assert flushed[0].session_id == "sess_researcher"
        assert len(reg.get_pending_events("my-team")) == 0

    def test_get_teammate_name_by_session(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        reg.register_teammate("my-team", "researcher", "sess_researcher")
        assert reg.get_teammate_name_by_session("sess_researcher") == "researcher"
        assert reg.get_teammate_name_by_session("unknown") is None

    def test_get_team_name_by_session(self) -> None:
        reg = TeamRegistry()
        reg.register_teammate("my-team", "researcher", "sess_r")
        assert reg.get_team_name_by_session("sess_r") == "my-team"
        assert reg.get_team_name_by_session("unknown") is None

    def test_get_agent_id_unknown_team(self) -> None:
        reg = TeamRegistry()
        assert reg.get_agent_id("no-team", "nobody") is None

    def test_get_agent_id_unknown_teammate(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        assert reg.get_agent_id("my-team", "nobody") is None

    def test_get_all_teammates(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        reg.register_teammate("my-team", "researcher", "s1")
        reg.register_teammate("my-team", "architect", "s2")
        teammates = reg.get_all_teammates("my-team")
        assert len(teammates) == 2
        names = {t.teammate_name for t in teammates}
        assert names == {"researcher", "architect"}

    def test_get_all_teammates_empty_team(self) -> None:
        reg = TeamRegistry()
        assert reg.get_all_teammates("no-team") == []

    def test_flush_pending_events_empty(self) -> None:
        reg = TeamRegistry()
        assert reg.flush_pending_events("no-team") == []

    def test_try_match_pending_teammate(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        reg.register_teammate("my-team", "researcher", "pending_researcher")

        result = reg.try_match_pending_teammate("sess_actual")
        assert result is not None
        assert result == ("my-team", "researcher")
        # Should now be registered with real session_id
        assert reg.is_teammate_session("sess_actual")
        assert reg.get_teammate_name_by_session("sess_actual") == "researcher"

    def test_try_match_pending_teammate_no_pending(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        reg.register_teammate("my-team", "researcher", "sess_real")
        # No pending registrations
        assert reg.try_match_pending_teammate("sess_new") is None

    def test_try_match_pending_teammate_no_teams(self) -> None:
        reg = TeamRegistry()
        assert reg.try_match_pending_teammate("sess_new") is None

    def test_multiple_pending_teammates_matched_in_order(self) -> None:
        reg = TeamRegistry()
        reg.register_lead("my-team", "sess_lead")
        reg.register_teammate("my-team", "Ada", "pending_Ada")
        reg.register_teammate("my-team", "Sage", "pending_Sage")

        result1 = reg.try_match_pending_teammate("sess_1")
        assert result1 is not None
        # First pending gets matched (order may vary by dict iteration)
        matched_name_1 = result1[1]
        assert matched_name_1 in ("Ada", "Sage")

        result2 = reg.try_match_pending_teammate("sess_2")
        assert result2 is not None
        matched_name_2 = result2[1]
        assert matched_name_2 in ("Ada", "Sage")
        assert matched_name_1 != matched_name_2
