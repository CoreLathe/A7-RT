"""
IDEM-005: Telemetry Clarity for `_wild_counts`

Tests that wild count telemetry is consistent across different code paths.
Currently, two different event types are used for the same counter:
- `wild_suspension` (from dispatch.py when subagent raises SubagentError)
- `validate_failed` (from validate.py when schema validation fails)

This test documents the current behavior and can verify any consolidation fix.
"""

import tempfile
from pathlib import Path

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


class TestIDEM005TelemetryClarity:
    """Test wild count telemetry consistency."""

    def test_wild_count_same_counter_across_code_paths(self):
        """
        Verify that _wild_counts uses the same counter regardless of source.

        The counter should increment for both:
        1. SubagentError during dispatch (wild_suspension event)
        2. Schema validation failure (validate_failed event)
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

            # Node already has status NEAR from dispatch_node

            def mock_manager_hook(board, state):
                return None

            def mock_subagent_hook_success(node_id, role, ctx, **kwargs):
                return SubagentReturn(status=NodeStatus.PROVISIONAL, role=role, files={})

            harness = Harness(
                repo=repo,
                manager_hook=mock_manager_hook,
                subagent_hook=mock_subagent_hook_success,
                git_root=None,
            )

            state = ManagerState(
                current_stage_id=stage.stage_id,
                turn=1,
                manager_max_turns=25,
                drain_turn=20,
                mode=ManagerMode.AUTONOMOUS,
            )

            # Initially no wild counts
            assert "test.node" not in harness._wild_counts

            # Simulate a validation failure (via _suspend_wild path)
            # Note: signature is (node_id, state, error)
            new_state = harness._suspend_wild(
                "test.node", state, "Schema error: missing required field"
            )

            # Wild count should be incremented
            assert harness._wild_counts.get("test.node") == 1

            # Check the event type used (consolidated to wild_suspension with wild_source)
            events = repo.get_events()
            wild_events = [e for e in events if e.get("action") == "wild_suspension"]
            assert len(wild_events) == 1
            assert wild_events[0].get("wild_count") == 1
            assert wild_events[0].get("wild_source") == "schema"

    def test_telemetry_consolidation_opportunity(self):
        """
        Demonstrate the telemetry clarity gap.

        Both paths increment _wild_counts but emit different event types:
        - dispatch.py: 'wild_suspension'
        - validate.py: 'validate_failed'

        A fix would either:
        1. Use consistent event type (e.g., both use 'wild_suspension')
        2. Add 'wild_count_source' field to distinguish sources
        """
        # This test documents the expected behavior after a fix
        # Currently serves as documentation of the gap
        pass
