"""Tests for Agent Teams support in the event mapper."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Allow importing from hooks package
HOOKS_SRC = str(Path(__file__).resolve().parents[2] / "hooks" / "src")
if HOOKS_SRC not in sys.path:
    sys.path.insert(0, HOOKS_SRC)

from claude_office_hooks.event_mapper import map_event  # noqa: E402


class TestTeamContextTagging:
    """Tests that map_event includes team_name and teammate_name from env vars."""

    def test_no_team_env_vars_omits_team_fields(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event("session_start", {"source": "cli"}, "sess_1")
            assert result is not None
            assert "team_name" not in result["data"]
            assert "teammate_name" not in result["data"]

    def test_team_name_from_env(self) -> None:
        env = {"CLAUDE_CODE_TEAM_NAME": "my-team", "CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event("session_start", {"source": "cli"}, "sess_1")
            assert result is not None
            assert result["data"]["team_name"] == "my-team"

    def test_teammate_name_from_env(self) -> None:
        env = {
            "CLAUDE_CODE_TEAM_NAME": "my-team",
            "CLAUDE_CODE_AGENT_NAME": "researcher",
            "CLAUDE_PROJECT_DIR": "/tmp",
        }
        with patch.dict(os.environ, env, clear=False):
            result = map_event("session_start", {"source": "cli"}, "sess_1")
            assert result is not None
            assert result["data"]["teammate_name"] == "researcher"

    def test_teammate_name_from_raw_data_agent_name(self) -> None:
        env = {"CLAUDE_CODE_TEAM_NAME": "my-team", "CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "session_start",
                {"source": "cli", "agent_name": "architect"},
                "sess_1",
            )
            assert result is not None
            assert result["data"]["teammate_name"] == "architect"

    def test_team_context_on_pre_tool_use(self) -> None:
        env = {
            "CLAUDE_CODE_TEAM_NAME": "my-team",
            "CLAUDE_CODE_AGENT_NAME": "researcher",
            "CLAUDE_PROJECT_DIR": "/tmp",
        }
        with patch.dict(os.environ, env, clear=False):
            result = map_event(
                "pre_tool_use",
                {"tool_name": "Read", "tool_use_id": "tu_1"},
                "sess_1",
            )
            assert result is not None
            assert result["data"]["team_name"] == "my-team"
            assert result["data"]["teammate_name"] == "researcher"


class TestTeamHookEvents:
    """Tests for TeammateIdle and TaskCompleted hook event forwarding."""

    def test_teammate_idle_mapping(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "teammate_idle",
                {"teammate_name": "researcher", "team_name": "my-team"},
                "sess_1",
            )
            assert result is not None
            assert result["event_type"] == "teammate_idle"
            assert result["data"]["teammate_name"] == "researcher"
            assert result["data"]["team_name"] == "my-team"

    def test_task_completed_mapping(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "task_completed",
                {
                    "task_id": "task-001",
                    "task_subject": "Audit auth module",
                    "teammate_name": "researcher",
                    "team_name": "my-team",
                },
                "sess_1",
            )
            assert result is not None
            assert result["event_type"] == "task_completed"
            assert result["data"]["task_id"] == "task-001"
            assert result["data"]["task_subject"] == "Audit auth module"


class TestAgentToolTeamExtraction:
    """Tests for team_name/teammate_name extraction from Agent tool_input."""

    def test_agent_tool_with_team_name_extracts_fields(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "pre_tool_use",
                {
                    "tool_name": "Agent",
                    "tool_use_id": "tu_1",
                    "tool_input": {
                        "team_name": "my-team",
                        "name": "Ada",
                        "description": "Architecture review",
                        "prompt": "Review the code",
                        "subagent_type": "Ada",
                    },
                },
                "sess_1",
            )
            assert result is not None
            assert result["event_type"] == "subagent_start"
            assert result["data"]["team_name"] == "my-team"
            assert result["data"]["teammate_name"] == "Ada"
            assert result["data"]["agent_name"] == "Ada"
            assert result["data"]["agent_type"] == "Ada"

    def test_agent_tool_without_team_name_no_team_fields(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "pre_tool_use",
                {
                    "tool_name": "Agent",
                    "tool_use_id": "tu_1",
                    "tool_input": {
                        "description": "Search for bugs",
                        "prompt": "Find issues",
                    },
                },
                "sess_1",
            )
            assert result is not None
            assert "team_name" not in result["data"]
            assert "teammate_name" not in result["data"]


class TestTeammateSpawnedPostToolUse:
    """Tests for teammate_spawned PostToolUse handling."""

    def test_teammate_spawned_not_mapped_to_subagent_stop(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "post_tool_use",
                {
                    "tool_name": "Agent",
                    "tool_use_id": "tu_1",
                    "tool_input": {"team_name": "my-team", "name": "Ada"},
                    "tool_response": {
                        "status": "teammate_spawned",
                        "name": "Ada",
                        "team_name": "my-team",
                        "teammate_id": "Ada@my-team",
                    },
                },
                "sess_1",
            )
            assert result is not None
            # Should NOT be subagent_stop
            assert result["event_type"] == "post_tool_use"
            assert result["data"]["team_name"] == "my-team"
            assert result["data"]["teammate_name"] == "Ada"
            assert result["data"]["agent_id"] == "main"

    def test_regular_agent_post_tool_use_still_subagent_stop(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            os.environ.pop("CLAUDE_CODE_AGENT_NAME", None)
            result = map_event(
                "post_tool_use",
                {
                    "tool_name": "Agent",
                    "tool_use_id": "tu_1",
                    "tool_input": {"description": "Search"},
                    "tool_response": {"content": [{"type": "text", "text": "Done"}]},
                },
                "sess_1",
            )
            assert result is not None
            assert result["event_type"] == "subagent_stop"


class TestSendMessageFieldVariants:
    """Tests that SendMessage works with different field names."""

    def test_send_message_with_message_field(self) -> None:
        """Real Claude Code uses 'message' field."""
        env = {"CLAUDE_PROJECT_DIR": "/tmp"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLAUDE_CODE_TEAM_NAME", None)
            result = map_event(
                "pre_tool_use",
                {
                    "tool_name": "SendMessage",
                    "tool_use_id": "tu_1",
                    "tool_input": {
                        "to": "architect",
                        "message": "Found SQL injection",
                        "type": "message",
                        "recipient": "architect",
                    },
                },
                "sess_1",
            )
            assert result is not None
            # SendMessage is not remapped — stays as pre_tool_use
            assert result["event_type"] == "pre_tool_use"
            assert result["data"]["tool_name"] == "SendMessage"
            # tool_input preserved for backend to extract
            assert result["data"]["tool_input"]["message"] == "Found SQL injection"
