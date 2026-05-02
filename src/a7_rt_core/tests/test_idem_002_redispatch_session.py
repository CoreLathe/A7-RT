"""
IDEM-002: Test for missing session tracking in _handle_redispatch.

This test demonstrates that redispatch does not create session log entries,
which breaks resume detection and warm redispatch context recovery.
"""

import tempfile
from pathlib import Path

import pytest

from a7_rt_core.core.models import (
    ManagerMode,
    ManagerState,
    NodeStatus,
    NodeType,
    RedispatchAction,
    SubagentReturn,
    create_project,
    dispatch_node,
)
from a7_rt_core.harness.core import Harness
from a7_rt_core.storage.repository import Repository


class TestIDEM002RedispatchSession:
    """Test that redispatch properly tracks sessions for resume detection."""

    def test_redispatch_writes_session_start(self):
        """
        Verify redispatch creates session_start log entry.

        Expected failure: No session_start in log
        Expected after fix: session_start present with redispatch metadata
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(name="Test Project", description="Test")
            repo = Repository.init(root, project)

            # Create stage and node
            stage = repo.create_stage("test-stage")
            node = dispatch_node(
                node_id="test.node",
                stage_id=stage.stage_id,
                type=NodeType.FEATURE,
                description="Test node",
            )
            repo.add_node(node)

            # Set up node as suspended (ready for redispatch)
            repo.update_node(
                "test.node",
                status=NodeStatus.SUSPENDED,
                suspension_reason={"type": "near", "detail": "test suspension"},
                retry_count=0,
                retry_context=[],
            )

            # Create harness with mock subagent that succeeds
            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook(node_id, role, ctx, **kwargs):
                # Return a successful result
                return SubagentReturn(
                    status=NodeStatus.PROVISIONAL,
                    role=role,
                    files={},
                )

            harness = Harness(
                repo=repo,
                manager_hook=mock_manager_hook,
                subagent_hook=mock_subagent_hook,
                git_root=None,
            )

            state = ManagerState(
                current_stage_id=stage.stage_id,
                turn=5,
                max_turns=25,
                drain_turn=20,
                mode=ManagerMode.AUTONOMOUS,
            )

            # Create redispatch action
            action = RedispatchAction(
                node_id="test.node",
                role="builder",
                weight="lean",
                manager_note="Retry after suspension",
            )

            # Execute redispatch
            harness._handle_redispatch(action, state)

            # Verify session log was written
            sessions = repo.get_sessions_for_node("test.node")
            assert len(sessions) == 1, f"Expected 1 session, got {len(sessions)}"

            session = sessions[0]
            assert session.role == "builder", f"Expected role 'builder', got {session.role}"
            assert session.status == "completed", (
                f"Expected status 'completed', got {session.status}"
            )

            # Check session_start was written to log file
            session_log_path = repo._sessions_dir / "test.node.jsonl"
            assert session_log_path.exists(), "Session log file should exist"

            log_content = session_log_path.read_text()
            assert '"type": "session_start"' in log_content, "session_start should be in log"
            assert '"type": "session_end"' in log_content, "session_end should be in log"

    def test_redispatch_tracks_active_session(self):
        """
        Verify redispatch tracks session in _active_sessions during execution.

        This is important for crash detection - if harness crashes mid-redispatch,
        resume should see the active session and mark it interrupted.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(name="Test Project", description="Test")
            repo = Repository.init(root, project)

            stage = repo.create_stage("test-stage")
            node = dispatch_node(
                node_id="test.node",
                stage_id=stage.stage_id,
                type=NodeType.FEATURE,
                description="Test node",
            )
            repo.add_node(node)

            repo.update_node(
                "test.node",
                status=NodeStatus.SUSPENDED,
                suspension_reason={"type": "near", "detail": "test suspension"},
                retry_count=0,
                retry_context=[],
            )

            # Track if _active_sessions was set during redispatch
            active_session_captured = None

            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook(node_id, role, ctx, **kwargs):
                nonlocal active_session_captured
                # Capture _active_sessions state during subagent execution
                active_session_captured = dict(harness._active_sessions)
                return SubagentReturn(status=NodeStatus.PROVISIONAL, role=role, files={})

            harness = Harness(
                repo=repo,
                manager_hook=mock_manager_hook,
                subagent_hook=mock_subagent_hook,
                git_root=None,
            )

            state = ManagerState(
                current_stage_id=stage.stage_id,
                turn=1,
                max_turns=25,
                drain_turn=20,
                mode=ManagerMode.AUTONOMOUS,
            )

            action = RedispatchAction(
                node_id="test.node",
                role="builder",
                weight="lean",
                manager_note="Test",
            )

            harness._handle_redispatch(action, state)

            # Verify session was tracked during execution
            assert active_session_captured is not None, "Subagent should have been called"
            assert "test.node" in active_session_captured, (
                f"Node should be in _active_sessions during execution. "
                f"Got: {active_session_captured}"
            )

            # After completion, should be cleared
            assert "test.node" not in harness._active_sessions, (
                "Node should be removed from _active_sessions after completion"
            )

    def test_redispatch_resume_marks_interrupted(self):
        """
        Verify interrupted redispatch is properly recovered on resume.

        If harness crashes during redispatch, resume should:
        1. Detect the "running" session (no session_end)
        2. Write session_end with status=interrupted
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(name="Test Project", description="Test")
            repo = Repository.init(root, project)

            stage = repo.create_stage("test-stage")
            node = dispatch_node(
                node_id="test.node",
                stage_id=stage.stage_id,
                type=NodeType.FEATURE,
                description="Test node",
            )
            repo.add_node(node)

            repo.update_node(
                "test.node",
                status=NodeStatus.SUSPENDED,
                suspension_reason={"type": "near", "detail": "test suspension"},
                retry_count=1,
                retry_context=[{"turn": 1, "attempt": 1, "manager_note": "First try"}],
            )

            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook(node_id, role, ctx, **kwargs):
                return SubagentReturn(status=NodeStatus.PROVISIONAL, role=role, files={})

            harness = Harness(
                repo=repo,
                manager_hook=mock_manager_hook,
                subagent_hook=mock_subagent_hook,
                git_root=None,
            )

            state = ManagerState(
                current_stage_id=stage.stage_id,
                turn=2,
                max_turns=25,
                drain_turn=20,
                mode=ManagerMode.AUTONOMOUS,
            )

            action = RedispatchAction(
                node_id="test.node",
                role="builder",
                weight="lean",
                manager_note="Second try",
            )

            # Simulate a crash: execute redispatch but don't let it complete normally
            # We'll manually inject a "running" session without session_end
            # This simulates what would happen if harness crashed mid-redispatch
            # with proper session tracking

            # First, manually write a session_start (as proper implementation would)
            import json
            from datetime import datetime, timezone

            session_id = "test-session-123"
            session_start = {
                "type": "session_start",
                "session_id": session_id,
                "node_id": "test.node",
                "role": "builder",
                "dispatch_turn": 2,
                "attempt_number": 2,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            repo.append_session_log("test.node", session_start)

            # Now verify get_active_session_for_node sees it as running
            active = repo.get_active_session_for_node("test.node")
            assert active is not None, "Should detect active session (no session_end)"
            assert active.status == "running", f"Expected status 'running', got {active.status}"

            # Create a manager state file to simulate an interrupted session
            # This is required for init_session to enter "resumed" mode
            from a7_rt_core.core.models import ManagerState as MS

            checkpoint_state = MS(
                current_stage_id=stage.stage_id,
                turn=2,
                max_turns=25,
                drain_turn=20,
                mode=ManagerMode.AUTONOMOUS,
                in_flight=["test.node"],  # Node was in flight when crash occurred
            )
            repo.manager_checkpoint(checkpoint_state)

            # Now simulate resume with a new harness instance
            new_harness = Harness(
                repo=repo,
                manager_hook=mock_manager_hook,
                subagent_hook=mock_subagent_hook,
                git_root=None,
            )

            # Resume should mark the interrupted session
            resumed_state = new_harness.init_session(stage.stage_id)

            # Check that session is now marked interrupted
            sessions = repo.get_sessions_for_node("test.node")
            interrupted = [s for s in sessions if s.status == "interrupted"]
            assert len(interrupted) == 1, (
                f"Should have 1 interrupted session, got {len(interrupted)}"
            )


def test_idem_002_regression():
    """Regression test for IDEM-002."""
    test = TestIDEM002RedispatchSession()
    test.test_redispatch_writes_session_start()
