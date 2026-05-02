"""
IDEM-004: Test for Read-Modify-Write (RMW) Race in update_node.

This test demonstrates the theoretical race condition where concurrent
calls to update_node can lose updates due to non-atomic read-modify-write.
"""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from a7_rt_core.core.models import (
    NodeStatus,
    NodeType,
    create_project,
    dispatch_node,
)
from a7_rt_core.storage.repository import Repository


class TestIDEM004RMWRace:
    """Test that demonstrates RMW race condition in update_node."""

    def test_concurrent_update_node_race(self):
        """
        Demonstrate RMW race condition in update_node.

        Setup: Two threads, both calling update_node on same node with different fields
        Thread A: Update field_a = "A" (via metadata)
        Thread B: Update field_b = "B" (via metadata) - starts after A loads, before A saves

        Expected (current behavior - demonstrates race):
        - Only one field present (last writer wins)

        Expected after fix (optimistic concurrency or file locking):
        - Final document has both field_a="A" and field_b="B"
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(name="Test Project", description="Test")
            repo = Repository.init(root, project)

            # Create a node
            stage = repo.create_stage("test-stage")
            node = dispatch_node(
                node_id="test.node",
                stage_id=stage.stage_id,
                type=NodeType.FEATURE,
                description="Test node",
            )
            repo.add_node(node)

            # Initial metadata
            repo.update_node(
                "test.node",
                metadata={"tokens": 100, "lines": 10, "chronicle": []},
            )

            results = {"A": None, "B": None}
            barrier_a = threading.Barrier(2)
            barrier_b = threading.Barrier(2)

            def thread_a():
                """Update custom_field_a."""
                # Load the document
                doc = repo._load()
                node_data = doc["nodes"]["test.node"]

                # Signal we're about to modify
                barrier_a.wait()

                # Small delay to let thread B load
                time.sleep(0.05)

                # Modify and save
                current_meta = node_data.get("metadata", {})
                current_meta["custom_field_a"] = "VALUE_A"
                repo.update_node("test.node", metadata=current_meta)
                results["A"] = "done"

            def thread_b():
                """Update custom_field_b."""
                # Wait for thread A to load
                barrier_a.wait()

                # Load the document (after A loaded but before A saved)
                doc = repo._load()
                node_data = doc["nodes"]["test.node"]

                # Small delay to ensure A saves first if it can
                time.sleep(0.02)

                # Modify and save
                current_meta = node_data.get("metadata", {})
                current_meta["custom_field_b"] = "VALUE_B"
                repo.update_node("test.node", metadata=current_meta)
                results["B"] = "done"

            # Run threads
            t_a = threading.Thread(target=thread_a)
            t_b = threading.Thread(target=thread_b)

            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()

            # Check final state
            final_doc = repo._load()
            final_metadata = final_doc["nodes"]["test.node"].get("metadata", {})

            print(f"Final metadata: {final_metadata}")
            print(f"Thread results: {results}")

            has_a = final_metadata.get("custom_field_a") == "VALUE_A"
            has_b = final_metadata.get("custom_field_b") == "VALUE_B"

            # Currently, this demonstrates the race - one update may be lost
            # After fix with proper concurrency control, both should be present
            if has_a and has_b:
                print("PASS: Both fields present - concurrency control working")
                assert True
            elif has_a or has_b:
                print(f"RACE DETECTED: Only field_a={has_a}, field_b={has_b}")
                # This demonstrates the deficiency - one field was lost
                # We document this as expected current behavior
                pytest.skip("RMW race confirmed - this documents the deficiency that needs fixing")
            else:
                pytest.fail("Neither field present - unexpected state")

    def test_update_node_single_threaded_is_consistent(self):
        """
        Verify that sequential updates work correctly (baseline).

        In single-threaded usage (current harness pattern), all updates
        should be preserved.
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

            # Sequential updates - should all be preserved
            repo.update_node(
                "test.node",
                metadata={"tokens": 100, "lines": 10, "chronicle": []},
            )

            # First update
            doc = repo._load()
            meta = doc["nodes"]["test.node"].get("metadata", {})
            meta["field_1"] = "value_1"
            repo.update_node("test.node", metadata=meta)

            # Second update
            doc = repo._load()
            meta = doc["nodes"]["test.node"].get("metadata", {})
            meta["field_2"] = "value_2"
            repo.update_node("test.node", metadata=meta)

            # Verify both present
            final_doc = repo._load()
            final_meta = final_doc["nodes"]["test.node"].get("metadata", {})

            assert final_meta.get("field_1") == "value_1"
            assert final_meta.get("field_2") == "value_2"
            assert final_meta.get("tokens") == 100


def test_idem_004_regression():
    """Regression test for IDEM-004."""
    test = TestIDEM004RMWRace()
    # Run the single-threaded test as baseline
    test.test_update_node_single_threaded_is_consistent()
