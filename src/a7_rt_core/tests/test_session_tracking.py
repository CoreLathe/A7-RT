"""
Tests for session tracking and warm redispatch functionality.

Covers:
- ExplorationEntry and AgentSession models
- Repository session log methods
- Session lifecycle (start, tool calls, end)
- Exploration hints (deduplication, discovery spread)
- Active session tracking
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from a7_rt_core.core.models import (
    AgentSession,
    ExplorationEntry,
    NodeStatus,
    SubagentReturn,
    create_project,
)
from a7_rt_core.storage.repository import Repository


class TestExplorationEntry:
    """Tests for the ExplorationEntry model."""

    def test_creation(self):
        entry = ExplorationEntry(
            tool="read_file",
            args={"path": "auth/jwt.py"},
            turn=5,
            timestamp="2026-04-12T10:01:00Z",
            intent="Understand JWT verification",
        )
        assert entry.tool == "read_file"
        assert entry.args == {"path": "auth/jwt.py"}
        assert entry.turn == 5
        assert entry.intent == "Understand JWT verification"

    def test_optional_intent(self):
        entry = ExplorationEntry(
            tool="grep_content",
            args={"pattern": "def verify"},
            turn=6,
            timestamp="2026-04-12T10:02:00Z",
        )
        assert entry.intent is None

    def test_model_frozen(self):
        entry = ExplorationEntry(
            tool="list_files",
            args={"glob": "**/*.py"},
            turn=1,
            timestamp="2026-04-12T10:00:00Z",
        )
        # Model is frozen, should not allow modification
        with pytest.raises(Exception):
            entry.tool = "read_file"


class TestAgentSession:
    """Tests for the AgentSession model."""

    def test_creation(self):
        session = AgentSession(
            session_id="sess-abc-123",
            node_id="auth.handler",
            dispatch_turn=5,
            role="builder",
            attempt_number=2,
            status="running",
            iterations_used=0,
            exploration_log=[],
            created_at="2026-04-12T10:00:00Z",
        )
        assert session.session_id == "sess-abc-123"
        assert session.node_id == "auth.handler"
        assert session.status == "running"

    def test_with_files_written(self):
        """Test AgentSession tracks files_written (grounded outputs only)."""
        session = AgentSession(
            session_id="sess-xyz-789",
            node_id="auth.handler",
            dispatch_turn=5,
            role="builder",
            attempt_number=1,
            status="completed",
            iterations_used=3,
            files_written=["auth/jwt.py", "auth/handler.py"],
            created_at="2026-04-12T10:00:00Z",
            ended_at="2026-04-12T10:05:00Z",
        )
        assert len(session.files_written) == 2
        assert session.files_written[0] == "auth/jwt.py"


class TestRepositorySessionLog:
    """Tests for Repository session logging methods."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(
                name="TestProject", description="Test session tracking"
            )
            repo = Repository.init(root, project)
            yield repo

    def test_append_session_log_creates_file(self, temp_repo):
        """Test that appending creates the session log file."""
        temp_repo.append_session_log(
            "test-node",
            {
                "type": "session_start",
                "session_id": "sess-123",
                "node_id": "test-node",
                "role": "builder",
                "dispatch_turn": 1,
                "attempt_number": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        session_file = temp_repo._sessions_dir / "test-node.jsonl"
        assert session_file.exists()

    def test_get_sessions_for_node_empty(self, temp_repo):
        """Test getting sessions for a node with no history."""
        sessions = temp_repo.get_sessions_for_node("nonexistent-node")
        assert sessions == []

    def test_get_sessions_for_node_single_session(self, temp_repo):
        """Test parsing a single complete session."""
        now = datetime.now(timezone.utc)

        # Write session start
        temp_repo.append_session_log(
            "auth.handler",
            {
                "type": "session_start",
                "session_id": "sess-abc-123",
                "node_id": "auth.handler",
                "role": "builder",
                "dispatch_turn": 5,
                "attempt_number": 1,
                "created_at": now.isoformat(),
            },
        )

        # Write tool call
        temp_repo.append_session_log(
            "auth.handler",
            {
                "type": "tool_call",
                "session_id": "sess-abc-123",
                "turn": 5,
                "tool": "read_file",
                "args": {"path": "auth/jwt.py"},
                "timestamp": now.isoformat(),
                "intent": "Understand JWT",
            },
        )

        # Write session end
        temp_repo.append_session_log(
            "auth.handler",
            {
                "type": "session_end",
                "session_id": "sess-abc-123",
                "status": "completed",
                "iterations_used": 3,
                "files_written": ["auth/handler.py"],
                "ended_at": now.isoformat(),
            },
        )

        sessions = temp_repo.get_sessions_for_node("auth.handler")
        assert len(sessions) == 1

        sess = sessions[0]
        assert sess.session_id == "sess-abc-123"
        assert sess.node_id == "auth.handler"
        assert sess.role == "builder"
        assert sess.status == "completed"
        assert sess.iterations_used == 3
        assert sess.files_written == ["auth/handler.py"]

    def test_get_sessions_multiple_sessions(self, temp_repo):
        """Test parsing multiple sessions for the same node."""
        now = datetime.now(timezone.utc)

        # First session
        temp_repo.append_session_log(
            "greet",
            {
                "type": "session_start",
                "session_id": "sess-1",
                "node_id": "greet",
                "role": "builder",
                "dispatch_turn": 1,
                "attempt_number": 1,
                "created_at": now.isoformat(),
            },
        )
        temp_repo.append_session_log(
            "greet",
            {
                "type": "session_end",
                "session_id": "sess-1",
                "status": "suspended",
                "iterations_used": 5,
                "ended_at": now.isoformat(),
            },
        )

        # Second session
        temp_repo.append_session_log(
            "greet",
            {
                "type": "session_start",
                "session_id": "sess-2",
                "node_id": "greet",
                "role": "builder",
                "dispatch_turn": 3,
                "attempt_number": 2,
                "created_at": now.isoformat(),
            },
        )
        temp_repo.append_session_log(
            "greet",
            {
                "type": "session_end",
                "session_id": "sess-2",
                "status": "completed",
                "iterations_used": 3,
                "files_written": ["auth/handler.py"],
                "ended_at": now.isoformat(),
            },
        )

        sessions = temp_repo.get_sessions_for_node("greet")
        assert len(sessions) == 2

        # Should be in order of appearance
        assert sessions[0].attempt_number == 1
        assert sessions[0].status == "suspended"
        assert sessions[1].attempt_number == 2
        assert sessions[1].status == "completed"

    def test_count_sessions_for_node(self, temp_repo):
        """Test counting sessions."""
        now = datetime.now(timezone.utc)

        assert temp_repo.count_sessions_for_node("greet") == 0

        # Add two sessions
        for i in range(2):
            temp_repo.append_session_log(
                "greet",
                {
                    "type": "session_start",
                    "session_id": f"sess-{i}",
                    "node_id": "greet",
                    "role": "builder",
                    "dispatch_turn": i,
                    "attempt_number": i + 1,
                    "created_at": now.isoformat(),
                },
            )
            temp_repo.append_session_log(
                "greet",
                {
                    "type": "session_end",
                    "session_id": f"sess-{i}",
                    "status": "completed",
                    "iterations_used": 1,
                    "ended_at": now.isoformat(),
                },
            )

        assert temp_repo.count_sessions_for_node("greet") == 2

    def test_get_active_session_for_node(self, temp_repo):
        """Test finding active (running) session."""
        now = datetime.now(timezone.utc)

        # No active session initially
        assert temp_repo.get_active_session_for_node("greet") is None

        # Add running session
        temp_repo.append_session_log(
            "greet",
            {
                "type": "session_start",
                "session_id": "sess-active",
                "node_id": "greet",
                "role": "builder",
                "dispatch_turn": 1,
                "attempt_number": 1,
                "created_at": now.isoformat(),
            },
        )

        active = temp_repo.get_active_session_for_node("greet")
        assert active is not None
        assert active.session_id == "sess-active"
        assert active.status == "running"

        # Complete the session
        temp_repo.append_session_log(
            "greet",
            {
                "type": "session_end",
                "session_id": "sess-active",
                "status": "completed",
                "iterations_used": 3,
                "ended_at": now.isoformat(),
            },
        )

        # No longer active
        assert temp_repo.get_active_session_for_node("greet") is None

    def test_get_session_by_id(self, temp_repo):
        """Test finding session by ID across all nodes."""
        now = datetime.now(timezone.utc)

        # Add sessions to different nodes
        temp_repo.append_session_log(
            "node-a",
            {
                "type": "session_start",
                "session_id": "sess-a",
                "node_id": "node-a",
                "role": "builder",
                "dispatch_turn": 1,
                "attempt_number": 1,
                "created_at": now.isoformat(),
            },
        )
        temp_repo.append_session_log(
            "node-a",
            {
                "type": "session_end",
                "session_id": "sess-a",
                "status": "completed",
                "iterations_used": 1,
                "ended_at": now.isoformat(),
            },
        )

        temp_repo.append_session_log(
            "node-b",
            {
                "type": "session_start",
                "session_id": "sess-b",
                "node_id": "node-b",
                "role": "test_author",
                "dispatch_turn": 2,
                "attempt_number": 1,
                "created_at": now.isoformat(),
            },
        )
        temp_repo.append_session_log(
            "node-b",
            {
                "type": "session_end",
                "session_id": "sess-b",
                "status": "completed",
                "iterations_used": 2,
                "ended_at": now.isoformat(),
            },
        )

        # Find each session by ID
        sess_a = temp_repo.get_session_by_id("sess-a")
        assert sess_a is not None
        assert sess_a.node_id == "node-a"
        assert sess_a.role == "builder"

        sess_b = temp_repo.get_session_by_id("sess-b")
        assert sess_b is not None
        assert sess_b.node_id == "node-b"
        assert sess_b.role == "test_author"

        # Nonexistent session
        assert temp_repo.get_session_by_id("sess-nonexistent") is None

    def test_get_exploration_hints_empty(self, temp_repo):
        """Test exploration hints for node with no history."""
        hints = temp_repo.get_exploration_hints("new-node")
        assert hints["previous_attempts"] == 0
        assert hints["files_written"] == []

    def test_get_exploration_hints_files_written(self, temp_repo):
        """Test that exploration hints collect files_written from sessions."""
        now = datetime.now(timezone.utc)

        # Session 1: completed with files_written
        temp_repo.append_session_log(
            "handler",
            {
                "type": "session_start",
                "session_id": "sess-1",
                "node_id": "handler",
                "role": "builder",
                "dispatch_turn": 1,
                "attempt_number": 1,
                "created_at": now.isoformat(),
            },
        )
        temp_repo.append_session_log(
            "handler",
            {
                "type": "session_end",
                "session_id": "sess-1",
                "status": "completed",
                "iterations_used": 3,
                "files_written": ["auth/jwt.py", "auth/config.py"],
                "ended_at": now.isoformat(),
            },
        )

        # Session 2: suspended, only wrote one file
        temp_repo.append_session_log(
            "handler",
            {
                "type": "session_start",
                "session_id": "sess-2",
                "node_id": "handler",
                "role": "builder",
                "dispatch_turn": 5,
                "attempt_number": 2,
                "created_at": now.isoformat(),
            },
        )
        temp_repo.append_session_log(
            "handler",
            {
                "type": "session_end",
                "session_id": "sess-2",
                "status": "suspended",
                "iterations_used": 2,
                "files_written": ["auth/handler.py"],
                "ended_at": now.isoformat(),
            },
        )

        hints = temp_repo.get_exploration_hints("handler")

        assert hints["previous_attempts"] == 2
        # All unique files_written should be collected
        assert "auth/jwt.py" in hints["files_written"]
        assert "auth/config.py" in hints["files_written"]
        assert "auth/handler.py" in hints["files_written"]
        assert len(hints["files_written"]) == 3


class TestSubagentReturnSessionId:
    """Tests for SubagentReturn session_id field."""

    def test_session_id_optional(self):
        """Test that session_id is optional in SubagentReturn."""
        ret = SubagentReturn(status=NodeStatus.GROUNDED, role="builder")
        assert ret.session_id is None

    def test_session_id_set(self):
        """Test setting session_id in SubagentReturn."""
        ret = SubagentReturn(
            status=NodeStatus.GROUNDED,
            role="builder",
            session_id="sess-abc-123",
        )
        assert ret.session_id == "sess-abc-123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
