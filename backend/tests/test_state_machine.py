"""Tests for state machine logic."""

from datetime import datetime

from app.core.state_machine import OfficePhase, StateMachine
from app.models.agents import Agent, AgentState, BossState
from app.models.events import Event, EventData, EventType


def make_event(event_type: EventType, **data_kwargs: object) -> Event:
    """Helper to create an Event with given type and data fields."""
    return Event(
        event_type=event_type,
        session_id="test_session",
        timestamp=datetime.now(),
        data=EventData(**data_kwargs),  # type: ignore[arg-type]
    )


class TestStateMachineInit:
    """Tests for StateMachine initialization."""

    def test_initial_phase_is_empty(self) -> None:
        """Initial phase should be EMPTY."""
        sm = StateMachine()
        assert sm.phase == OfficePhase.EMPTY

    def test_initial_boss_state_is_idle(self) -> None:
        """Initial boss state should be IDLE."""
        sm = StateMachine()
        assert sm.boss_state == BossState.IDLE

    def test_initial_agents_empty(self) -> None:
        """Initial agents dict should be empty."""
        sm = StateMachine()
        assert len(sm.agents) == 0

    def test_initial_queues_empty(self) -> None:
        """Initial queues should be empty."""
        sm = StateMachine()
        assert len(sm.arrival_queue) == 0
        assert len(sm.handin_queue) == 0

    def test_initial_token_counts_zero(self) -> None:
        """Initial token counts should be zero."""
        sm = StateMachine()
        assert sm.total_input_tokens == 0
        assert sm.total_output_tokens == 0

    def test_initial_tool_uses_zero(self) -> None:
        """Initial tool uses counter should be zero."""
        sm = StateMachine()
        assert sm.tool_uses_since_compaction == 0


class TestRemoveAgent:
    """Tests for remove_agent method."""

    def test_remove_existing_agent(self) -> None:
        """Should remove agent from agents dict."""
        sm = StateMachine()
        from app.models.agents import Agent

        sm.agents["agent1"] = Agent(
            id="agent1", name="Test", color="#ff0000", number=1, state=AgentState.WORKING
        )
        sm.remove_agent("agent1")
        assert "agent1" not in sm.agents

    def test_remove_agent_from_arrival_queue(self) -> None:
        """Should remove agent from arrival queue."""
        sm = StateMachine()
        from app.models.agents import Agent

        sm.agents["agent1"] = Agent(
            id="agent1", name="Test", color="#ff0000", number=1, state=AgentState.ARRIVING
        )
        sm.arrival_queue.append("agent1")
        sm.remove_agent("agent1")
        assert "agent1" not in sm.arrival_queue

    def test_remove_agent_from_handin_queue(self) -> None:
        """Should remove agent from handin queue."""
        sm = StateMachine()
        from app.models.agents import Agent

        sm.agents["agent1"] = Agent(
            id="agent1", name="Test", color="#ff0000", number=1, state=AgentState.COMPLETED
        )
        sm.handin_queue.append("agent1")
        sm.remove_agent("agent1")
        assert "agent1" not in sm.handin_queue

    def test_remove_nonexistent_agent_no_error(self) -> None:
        """Removing nonexistent agent should not raise error."""
        sm = StateMachine()
        sm.remove_agent("nonexistent")  # Should not raise


class TestToGameState:
    """Tests for to_game_state method."""

    def test_returns_game_state_object(self) -> None:
        """Should return a GameState object."""
        sm = StateMachine()
        state = sm.to_game_state("test_session")
        assert state.session_id == "test_session"

    def test_boss_state_copied(self) -> None:
        """Boss state should be included in game state."""
        sm = StateMachine()
        sm.boss_state = BossState.WORKING
        state = sm.to_game_state("test")
        assert state.boss.state == BossState.WORKING

    def test_desk_count_minimum_8(self) -> None:
        """Desk count should be at least 8."""
        sm = StateMachine()
        state = sm.to_game_state("test")
        assert state.office.desk_count >= 8

    def test_desk_count_capped_at_max_agents(self) -> None:
        """Desk count should not exceed MAX_AGENTS."""
        sm = StateMachine()
        from app.models.agents import Agent

        # Add 10 agents (more than MAX_AGENTS=8)
        for i in range(10):
            sm.agents[f"agent{i}"] = Agent(
                id=f"agent{i}",
                name=f"Test{i}",
                color="#ff0000",
                number=i,
                state=AgentState.WORKING,
            )
        state = sm.to_game_state("test")
        assert state.office.desk_count == StateMachine.MAX_AGENTS

    def test_context_utilization_calculated(self) -> None:
        """Context utilization should be calculated from tokens."""
        sm = StateMachine()
        sm.total_input_tokens = 100_000
        sm.total_output_tokens = 50_000
        state = sm.to_game_state("test")
        # 150,000 / 200,000 = 0.75
        assert state.office.context_utilization == 0.75

    def test_context_utilization_capped_at_1(self) -> None:
        """Context utilization should be capped at 1.0."""
        sm = StateMachine()
        sm.total_input_tokens = 300_000
        sm.total_output_tokens = 100_000
        state = sm.to_game_state("test")
        assert state.office.context_utilization == 1.0

    def test_context_size_from_model(self) -> None:
        """Context window should be set from model ID on session_start."""
        sm = StateMachine()
        event = Event(
            event_type=EventType.SESSION_START,
            session_id="test",
            data=EventData(model="claude-opus-4-6[1m]"),
        )
        sm.transition(event)
        assert sm.max_context_tokens == 1_000_000
        # Utilization should use the larger window
        sm.total_input_tokens = 100_000
        sm.total_output_tokens = 50_000
        state = sm.to_game_state("test")
        assert state.office.context_utilization == 0.15

    def test_context_size_default_for_unknown_model(self) -> None:
        """Unknown models should use the default context window."""
        sm = StateMachine()
        event = Event(
            event_type=EventType.SESSION_START,
            session_id="test",
            data=EventData(model="claude-sonnet-4-6"),
        )
        sm.transition(event)
        assert sm.max_context_tokens == 200_000

    def test_queues_copied(self) -> None:
        """Queues should be copied to game state."""
        sm = StateMachine()
        sm.arrival_queue = ["a1", "a2"]
        sm.handin_queue = ["a3"]
        state = sm.to_game_state("test")
        assert state.arrival_queue == ["a1", "a2"]
        assert state.departure_queue == ["a3"]

    def test_tool_uses_included(self) -> None:
        """Tool uses counter should be in office state."""
        sm = StateMachine()
        sm.tool_uses_since_compaction = 42
        state = sm.to_game_state("test")
        assert state.office.tool_uses_since_compaction == 42

    def test_print_report_included(self) -> None:
        """Print report flag should be in office state."""
        sm = StateMachine()
        sm.print_report = True
        state = sm.to_game_state("test")
        assert state.office.print_report is True


class TestOfficePhase:
    """Tests for OfficePhase enum."""

    def test_all_phases_exist(self) -> None:
        """All expected phases should exist."""
        phases = [
            OfficePhase.EMPTY,
            OfficePhase.STARTING,
            OfficePhase.IDLE,
            OfficePhase.WORKING,
            OfficePhase.DELEGATING,
            OfficePhase.BUSY,
            OfficePhase.COMPLETING,
            OfficePhase.ENDED,
        ]
        assert len(phases) == 8

    def test_phases_are_unique(self) -> None:
        """All phases should have unique values."""
        values = [p.value for p in OfficePhase]
        assert len(values) == len(set(values))


class TestTeamStateMachine:
    """Tests for Agent Teams state machine extensions."""

    def test_initial_team_state(self) -> None:
        sm = StateMachine()
        assert sm.team_name is None
        assert len(sm.teammate_agents) == 0

    def test_teammate_message_sets_sender_bubble(self) -> None:
        sm = StateMachine()
        sm.agents["teammate_researcher"] = Agent(
            id="teammate_researcher",
            name="researcher",
            color="#3B82F6",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
        )
        sm.agents["teammate_architect"] = Agent(
            id="teammate_architect",
            name="architect",
            color="#22C55E",
            number=2,
            state=AgentState.WORKING,
            is_teammate=True,
        )
        sm.teammate_agents = {"teammate_researcher", "teammate_architect"}

        event = make_event(
            EventType.TEAMMATE_MESSAGE,
            agent_id="teammate_researcher",
            teammate_name="researcher",
            message_to="architect",
            message_text="Found a bug in auth module",
        )
        sm.transition(event)

        assert sm.agents["teammate_researcher"].bubble is not None
        assert "Found a bug" in sm.agents["teammate_researcher"].bubble.text
        assert sm.agents["teammate_architect"].bubble is not None
        assert "researcher" in sm.agents["teammate_architect"].bubble.text

    def test_teammate_idle_sets_waiting_state(self) -> None:
        sm = StateMachine()
        sm.agents["teammate_researcher"] = Agent(
            id="teammate_researcher",
            name="researcher",
            color="#3B82F6",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
        )
        sm.teammate_agents = {"teammate_researcher"}

        event = make_event(EventType.TEAMMATE_IDLE, teammate_name="researcher")
        sm.transition(event)
        assert sm.agents["teammate_researcher"].state == AgentState.WAITING

    def test_task_completed_increments_counter_and_adds_todo(self) -> None:
        sm = StateMachine()
        initial_count = sm.whiteboard.task_completed_count

        event = make_event(
            EventType.TASK_COMPLETED,
            teammate_name="researcher",
            task_subject="Audit auth module",
        )
        sm.transition(event)
        assert sm.whiteboard.task_completed_count == initial_count + 1
        # Should also add a completed todo item
        assert any(t.content == "Audit auth module" and t.status == "completed" for t in sm.todos)

    def testfind_agent_by_teammate_name(self) -> None:
        sm = StateMachine()
        sm.agents["teammate_researcher"] = Agent(
            id="teammate_researcher",
            name="researcher",
            color="#3B82F6",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
        )
        sm.teammate_agents = {"teammate_researcher"}
        assert sm.find_agent_by_teammate_name("researcher") == "teammate_researcher"
        assert sm.find_agent_by_teammate_name("nonexistent") is None


class TestTeammateAgentCreation:
    """Tests for teammate-aware agent creation."""

    def test_subagent_start_with_is_teammate_flag(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.SUBAGENT_START,
            agent_id="teammate_researcher",
            agent_name="researcher",
            task_description="Team member: researcher",
            is_teammate=True,
        )
        sm.transition(event)

        assert "teammate_researcher" in sm.agents
        assert sm.agents["teammate_researcher"].is_teammate is True

    def test_regular_subagent_is_not_teammate(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.SUBAGENT_START,
            agent_id="subagent_abc",
            agent_name="helper",
            task_description="Do something",
        )
        sm.transition(event)

        assert "subagent_abc" in sm.agents
        assert sm.agents["subagent_abc"].is_teammate is False


class TestTeammateMessageBoss:
    """Tests for boss as sender/recipient in TEAMMATE_MESSAGE."""

    def test_boss_sender_sets_boss_bubble(self) -> None:
        sm = StateMachine()
        sm.agents["teammate_researcher"] = Agent(
            id="teammate_researcher",
            name="researcher",
            color="#3B82F6",
            number=1,
            state=AgentState.WORKING,
            is_teammate=True,
        )
        sm.teammate_agents = {"teammate_researcher"}

        event = make_event(
            EventType.TEAMMATE_MESSAGE,
            agent_id="main",
            message_to="researcher",
            message_text="Focus on SQL injection",
        )
        sm.transition(event)

        # Boss bubble should be set
        assert sm.boss_bubble is not None
        assert "SQL injection" in sm.boss_bubble.text
        # Recipient should show listening
        assert sm.agents["teammate_researcher"].bubble is not None
        assert "Team Lead" in sm.agents["teammate_researcher"].bubble.text

    def test_message_text_truncated_at_40_chars(self) -> None:
        sm = StateMachine()
        sm.agents["teammate_a"] = Agent(
            id="teammate_a", name="a", color="#fff", number=1,
            state=AgentState.WORKING, is_teammate=True,
        )
        sm.teammate_agents = {"teammate_a"}

        long_text = "x" * 60
        event = make_event(
            EventType.TEAMMATE_MESSAGE,
            agent_id="main",
            message_to="a",
            message_text=long_text,
        )
        sm.transition(event)
        assert sm.boss_bubble is not None
        assert len(sm.boss_bubble.text) <= 43  # 40 + "..."

    def test_message_to_unknown_recipient_no_crash(self) -> None:
        sm = StateMachine()
        sm.teammate_agents = set()
        event = make_event(
            EventType.TEAMMATE_MESSAGE,
            agent_id="main",
            message_to="nobody",
            message_text="Hello",
        )
        sm.transition(event)  # Should not crash
        assert sm.boss_bubble is not None


class TestTaskCreateUpdate:
    """Tests for TaskCreate and TaskUpdate tool handling."""

    def test_task_create_adds_pending_todo(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            tool_name="TaskCreate",
            tool_input={"subject": "Fix auth bug", "description": "Patch login"},
        )
        sm.transition(event)
        assert any(t.content == "Fix auth bug" and t.status == "pending" for t in sm.todos)

    def test_task_create_with_active_form(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            tool_name="TaskCreate",
            tool_input={"subject": "Run tests", "activeForm": "Running tests"},
        )
        sm.transition(event)
        todo = next(t for t in sm.todos if t.content == "Run tests")
        assert todo.active_form == "Running tests"

    def test_task_create_empty_subject_ignored(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            tool_name="TaskCreate",
            tool_input={"subject": "", "description": "Empty"},
        )
        sm.transition(event)
        assert len(sm.todos) == 0

    def test_task_update_changes_status(self) -> None:
        sm = StateMachine()
        from app.models.common import TodoItem, TodoStatus

        sm.todos = [TodoItem(content="Task A", status=TodoStatus.PENDING)]

        event = make_event(
            EventType.PRE_TOOL_USE,
            tool_name="TaskUpdate",
            tool_input={"taskId": "1", "status": "in_progress"},
        )
        sm.transition(event)
        assert sm.todos[0].status == TodoStatus.IN_PROGRESS

    def test_task_update_changes_subject(self) -> None:
        sm = StateMachine()
        from app.models.common import TodoItem, TodoStatus

        sm.todos = [TodoItem(content="Old name", status=TodoStatus.PENDING)]

        event = make_event(
            EventType.PRE_TOOL_USE,
            tool_name="TaskUpdate",
            tool_input={"taskId": "1", "subject": "New name"},
        )
        sm.transition(event)
        assert sm.todos[0].content == "New name"

    def test_task_update_invalid_task_id_no_crash(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.PRE_TOOL_USE,
            tool_name="TaskUpdate",
            tool_input={"taskId": "999", "status": "completed"},
        )
        sm.transition(event)  # Should not crash

    def test_task_completed_matches_by_index(self) -> None:
        sm = StateMachine()
        from app.models.common import TodoItem, TodoStatus

        sm.todos = [
            TodoItem(content="Task A", status=TodoStatus.IN_PROGRESS),
            TodoItem(content="Task B", status=TodoStatus.PENDING),
        ]

        event = make_event(
            EventType.TASK_COMPLETED,
            task_id="1",
            task_subject="Task A",
            teammate_name="researcher",
        )
        sm.transition(event)
        assert sm.todos[0].status == TodoStatus.COMPLETED
        assert sm.todos[1].status == TodoStatus.PENDING

    def test_task_completed_matches_by_subject_fallback(self) -> None:
        sm = StateMachine()
        from app.models.common import TodoItem, TodoStatus

        sm.todos = [TodoItem(content="Fix auth", status=TodoStatus.IN_PROGRESS)]

        event = make_event(
            EventType.TASK_COMPLETED,
            task_id="not-a-number",
            task_subject="Fix auth",
            teammate_name="researcher",
        )
        sm.transition(event)
        assert sm.todos[0].status == TodoStatus.COMPLETED

    def test_task_completed_creates_new_if_not_found(self) -> None:
        sm = StateMachine()
        event = make_event(
            EventType.TASK_COMPLETED,
            task_id="99",
            task_subject="New task",
            teammate_name="researcher",
        )
        sm.transition(event)
        assert any(t.content == "New task" and t.status == "completed" for t in sm.todos)
