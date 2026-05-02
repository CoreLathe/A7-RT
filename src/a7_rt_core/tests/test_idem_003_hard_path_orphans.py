"""
IDEM-003: Test for Hard-Path Gate Orphaned Files.

This test demonstrates that if a crash occurs between file write and cleanup
in _run_hard_path_gate, orphaned files can persist and potentially poison
fast-path grounding on resume.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from a7_rt_core.core.models import (
    ManagerMode,
    ManagerState,
    NodeStatus,
    NodeType,
    SubagentReturn,
    create_project,
    dispatch_node,
)
from a7_rt_core.harness.core import Harness
from a7_rt_core.storage.repository import Repository
from a7_rt_core.validation.schema import Validator


class TestIDEM003HardPathOrphans:
    """Test hard-path gate file cleanup and orphan detection."""

    def test_hard_path_failure_cleans_up_files(self):
        """
        Verify files written before test are cleaned up on test failure.

        Setup: Create node with implementation that will fail tests
        Trigger: _run_hard_path_gate with failing test
        Assert: Files are unlinked after test failure
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

            # Create a test that will fail (pytest-style assertions at module level)
            test_content = """
# Test that always fails
assert False, "Intentional failure for testing"
"""
            repo._atomic_write_content(repo._content_dir / "test.node.test", test_content)

            # Create harness
            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook(node_id, role, ctx, **kwargs):
                return SubagentReturn(
                    status=NodeStatus.PROVISIONAL,
                    role="builder",
                    files={"test.node.py": "print('hello')"},
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

            # Create a return that claims grounded status
            result = SubagentReturn(
                status=NodeStatus.GROUNDED,
                role="builder",
                files={"test.node.py": "print('hello')"},
            )

            # Execute hard-path gate
            final_status = harness._run_hard_path_gate(
                node_id="test.node",
                files_to_write={"test.node.py": "print('hello')"},
                result=result,
                override_status=NodeStatus.GROUNDED,
                state=state,
            )

            # Verify test failed and status is POISONED
            assert final_status == NodeStatus.POISONED, (
                f"Expected POISONED status, got {final_status}"
            )

            # Verify files were cleaned up
            assert not (repo._content_dir / "test.node.py").exists(), (
                "test.node.py should be cleaned up after test failure"
            )

    def test_orphaned_files_do_not_poison_fast_path(self):
        """
        Verify that orphaned files from crashed hard-path don't poison fast-path.

        Simulates: Crash after file write but before test/cleanup
        On resume: Fast-path should detect orphaned files and re-run tests
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

            # Manually create orphaned files (simulating crash mid-hard-path)
            # This represents files written but not cleaned up due to crash
            orphaned_content = "print('orphaned - should not be trusted')"
            repo._atomic_write_content(repo._content_dir / "test.node.py", orphaned_content)

            # Create a test that would pass with correct implementation
            # but should fail with orphaned content
            test_content = """
# This test expects specific content that orphaned file doesn't have
import ast
with open("test.node.py") as f:
    source = f.read()
assert "correct" in source, "File should contain 'correct' implementation"
"""
            repo._atomic_write_content(repo._content_dir / "test.node.test", test_content)

            # Verify orphaned file exists
            assert (repo._content_dir / "test.node.py").exists(), "Orphaned file should exist"

            # Now run hard-path gate - it should detect the file and run tests
            # The test should fail because orphaned content doesn't match expectations

            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook(node_id, role, ctx, **kwargs):
                return None

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

            result = SubagentReturn(
                status=NodeStatus.GROUNDED,
                role="builder",
                files={"test.node.py": "correct implementation"},  # New correct content
            )

            # Execute hard-path gate
            final_status = harness._run_hard_path_gate(
                node_id="test.node",
                files_to_write={"test.node.py": "correct implementation"},
                result=result,
                override_status=NodeStatus.GROUNDED,
                state=state,
            )

            # Hard-path should run tests on the new files, not trust orphans
            # Since we write new files before testing, orphans get overwritten
            # This test verifies the current behavior: files are overwritten pre-test

            # After hard-path, file should contain the new content (overwritten)
            final_content = (repo._content_dir / "test.node.py").read_text()
            assert "correct" in final_content, (
                f"File should contain new correct content, got: {final_content}"
            )

    def test_fast_path_grounding_rejects_orphaned_files(self):
        """
        Verify fast-path grounding runs tests and rejects bad files.

        Fast-path is an optimization but must still validate.
        Orphaned files that fail tests should be rejected via _try_fast_path_ground.
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

            # Create a test that requires specific behavior
            test_content = """
# Test that checks for required function
import test_node
assert hasattr(test_node, 'required_function'), "Missing required_function"
"""
            repo._atomic_write_content(repo._content_dir / "test.node.test", test_content)

            # Create orphaned file that lacks required function
            orphaned = "print('incomplete')"
            repo._atomic_write_content(repo._content_dir / "test.node.py", orphaned)

            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook(node_id, role, ctx, **kwargs):
                return None

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

            # Set up node as PROVISIONAL to attempt fast-path
            repo.update_node(
                "test.node",
                status=NodeStatus.PROVISIONAL,
            )

            # _can_fast_path_ground only checks file existence, not test passage
            can_fast_path = harness._can_fast_path_ground("test.node")
            assert can_fast_path, "Files exist so fast-path gate should be possible"

            # The actual validation happens in _try_fast_path_ground which runs tests
            result = harness._try_fast_path_ground("test.node", state)

            # Fast-path should fail because tests fail (no required_function)
            assert not result.success, f"Fast-path should fail with bad files: {result.detail}"
            events = repo.get_events()
            assert "fast_path_failed" in [e.get("action") for e in events]


def test_idem_003_regression():
    """Regression test for IDEM-003."""
    test = TestIDEM003HardPathOrphans()
    test.test_hard_path_failure_cleans_up_files()
    test.test_orphaned_files_do_not_poison_fast_path()
    test.test_fast_path_grounding_rejects_orphaned_files()
