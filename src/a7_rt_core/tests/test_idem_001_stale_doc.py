"""
IDEM-001: Test for stale document read in _persist_analyst_findings.

This test demonstrates that analyst findings persistence can clobber
metadata due to the interaction between update_node() and _log_chronicle_entry().
"""

import json
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


class TestIDEM001StaleDocRead:
    """Test that _persist_analyst_findings does not clobber prior metadata."""

    def test_analyst_findings_preserves_custom_metadata(self):
        """
        Demonstrate the metadata clobbering bug.

        When _persist_analyst_findings runs:
        1. It calls update_node() which saves metadata including custom fields
        2. It calls _log_chronicle_entry() which reloads and saves again
        3. _log_chronicle_entry only copies known fields, losing custom ones

        Expected failure: custom_field is lost
        Expected after fix: custom_field preserved
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(name="Test Project", description="Test")
            repo = Repository.init(root, project)

            # Create a stage and node
            stage = repo.create_stage("test-stage")
            node = dispatch_node(
                node_id="test.node",
                stage_id=stage.stage_id,
                type=NodeType.FEATURE,
                description="Test node",
            )
            repo.add_node(node)

            # Set up initial metadata with custom field and prior findings
            repo.update_node(
                "test.node",
                metadata={
                    "tokens": 100,
                    "lines": 10,
                    "chronicle": [],
                    "analyst_findings": [
                        {"node_id": "old", "findings": ["stale"], "confidence": "low"}
                    ],
                    "custom_field": "should_persist",
                },
            )

            # Create minimal harness
            harness = self._create_harness(repo)

            state = ManagerState(
                current_stage_id=stage.stage_id,
                turn=5,
                max_turns=25,
                drain_turn=20,
                mode=ManagerMode.AUTONOMOUS,
            )

            # Create analyst return (no escalation to isolate the core bug)
            result = SubagentReturn(
                status=NodeStatus.PROVISIONAL,
                role="analyst",
                analysis_result={
                    "scope": "node",
                    "target_nodes": ["test.node"],
                    "findings": ["Test finding"],
                    "confidence": "high",
                    "sources": ["test.py"],
                    "escalate": False,
                },
            )

            # Execute
            harness._persist_analyst_findings("test.node", result, state)

            # Verify
            final_doc = repo._load()
            final_metadata = final_doc["nodes"]["test.node"].get("metadata", {})

            print(f"Final metadata keys: {list(final_metadata.keys())}")

            # Core assertions
            assert "analyst_findings" in final_metadata, "analyst_findings should exist"
            # Ephemeral semantics: old findings replaced, not appended
            assert len(final_metadata["analyst_findings"]) == 1, (
                "Should have exactly one (new) finding"
            )
            assert final_metadata["analyst_findings"][0]["findings"] == ["Test finding"], (
                "Should be new finding, not old"
            )

            # custom_field should be preserved (extra='allow' fix)
            assert final_metadata.get("custom_field") == "should_persist", (
                f"custom_field lost! Keys present: {list(final_metadata.keys())}"
            )

            # Also verify standard fields preserved
            assert final_metadata.get("tokens") == 100
            assert final_metadata.get("lines") == 10

            # Verify chronicle was logged
            assert len(final_metadata.get("chronicle", [])) == 1, "Chronicle entry should exist"

    def _create_harness(self, repo):
        """Create minimal harness for testing."""

        def mock_manager_hook(board, state):
            return None

        def mock_subagent_hook(node_id, role, ctx, **kwargs):
            return None

        return Harness(
            repo=repo,
            manager_hook=mock_manager_hook,
            subagent_hook=mock_subagent_hook,
            git_root=None,
        )


def test_idem_001_regression():
    """Regression test for IDEM-001."""
    test = TestIDEM001StaleDocRead()
    test.test_analyst_findings_preserves_custom_metadata()
