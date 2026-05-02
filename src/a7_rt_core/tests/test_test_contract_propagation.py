"""
Mechanical test for test_contract propagation from test_author to builder.

Tests that when a node has test_contract set, builder_view() includes it
in the context passed to the builder.

Run standalone: python tests/test_test_contract_propagation.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a7_rt_core.agent.loop import AgentLoop
from a7_rt_core.context.core import builder_view
from a7_rt_core.core.models import NodeStatus, SubagentReturn, SuspensionType


def test_test_contract_propagation():
    """
    Test that test_contract flows from node to builder context.

    Scenario:
    - test_author has defined a contract and stored test_contract on node
    - builder is dispatched for the same node
    - builder_view() should include test_contract in the returned context
    """
    # Create a minimal master document with a node that has test_contract
    doc = {
        "project": {
            "project_id": "test-proj",
            "name": "Test Project",
            "description": "Test",
            "created_at": "2026-04-24T00:00:00Z",
            "max_turns": 100,
            "drain_turn": 80,
        },
        "stages": {
            "stage-1": {
                "stage_id": "stage-1",
                "name": "stage-1",
                "status": "active",
                "created_at": "2026-04-24T00:00:00Z",
                "node_ids": ["utils.errors"],
            }
        },
        "nodes": {
            "utils.errors": {
                "node_id": "utils.errors",
                "stage_id": "stage-1",
                "type": "feature",
                "status": "provisional",
                "protocol_weight": "lean",
                "description": "Standard exception hierarchy with structured error codes",
                "interface": {
                    "exports": [
                        "class BaseError(Exception): def __init__(self, code: str, message: str)",
                        "class ValidationError(BaseError): def __init__(self, message: str) -> code='VAL001'",
                    ],
                    "assumptions": [],
                    "raises": [],
                    "guarantees": [],
                },
                "structural_deps": [],
                "assumption_deps": [],
                "content_file": None,
                "suspension_reason": None,
                "poisoned_by": None,
                "retry_count": 0,
                "max_retries": 3,
                "pr_note": "Test author submission",
                # This is the key field - test_contract from test_author
                "test_contract": """TEST CONTRACT: utils.errors

API:
- class BaseError(Exception): def __init__(self, code: str, message: str)
- class ValidationError(BaseError): def __init__(self, message: str) -> code='VAL001'

BEHAVIOR:
- BaseError is root exception with code and message attributes
- ValidationError inherits from BaseError with predefined code 'VAL001'

EDGES:
- None

COERCION:
- None""",
                "committed_files": [],
                "tags": [],
                "view_spec": {},
                "metadata": {
                    "tokens": 0,
                    "lines": 0,
                    "content_hash": "",
                    "computed_at": "2026-04-24T00:00:00Z",
                },
            }
        },
        "dependencies": [],
        "graveyard": [],
    }

    # Call builder_view for the node
    view = builder_view(doc, "utils.errors")

    # Verify test_contract is present in builder context
    assert "test_contract" in view, (
        f"test_contract missing from builder view. Keys: {list(view.keys())}"
    )

    tc = view["test_contract"]
    assert tc is not None, "test_contract should not be None"
    assert "TEST CONTRACT: utils.errors" in tc, (
        f"test_contract header missing. Got: {tc[:100]}"
    )
    assert "BaseError" in tc, "BaseError not mentioned in test_contract"
    assert "ValidationError" in tc, "ValidationError not mentioned in test_contract"

    print("✓ test_contract propagated to builder view")
    print(f"✓ test_contract length: {len(tc)} chars")
    print(f"✓ test_contract preview: {tc[:200]}...")


def test_test_contract_absent_when_not_set():
    """
    Test that test_contract is not in builder view when node doesn't have it.

    This is the Mode A case where exports were already defined.
    """
    doc = {
        "project": {
            "project_id": "test-proj",
            "name": "Test Project",
            "description": "Test",
            "created_at": "2026-04-24T00:00:00Z",
            "max_turns": 100,
            "drain_turn": 80,
        },
        "stages": {
            "stage-1": {
                "stage_id": "stage-1",
                "name": "stage-1",
                "status": "active",
                "created_at": "2026-04-24T00:00:00Z",
                "node_ids": ["utils.validation"],
            }
        },
        "nodes": {
            "utils.validation": {
                "node_id": "utils.validation",
                "stage_id": "stage-1",
                "type": "feature",
                "status": "near",
                "protocol_weight": "lean",
                "description": "Validation utilities",
                "interface": {
                    "exports": ["def validate_email(email: str) -> bool"],
                    "assumptions": [],
                    "raises": [],
                    "guarantees": [],
                },
                "structural_deps": [],
                "assumption_deps": [],
                "content_file": None,
                "suspension_reason": None,
                "poisoned_by": None,
                "retry_count": 0,
                "max_retries": 3,
                "pr_note": None,
                # No test_contract - Mode A (exports already defined)
                "committed_files": [],
                "tags": [],
                "view_spec": {},
                "metadata": {
                    "tokens": 0,
                    "lines": 0,
                    "content_hash": "",
                    "computed_at": "2026-04-24T00:00:00Z",
                },
            }
        },
        "dependencies": [],
        "graveyard": [],
    }

    view = builder_view(doc, "utils.validation")

    # When test_contract is not set on node, it shouldn't be in view
    # (or should be None/empty)
    tc = view.get("test_contract")
    assert tc is None, f"test_contract should be None/omitted for Mode A, got: {tc}"

    print("✓ test_contract correctly absent for Mode A node")


def test_builder_view_structure():
    """
    Verify the overall structure of builder_view includes expected keys.
    """
    doc = {
        "project": {
            "project_id": "test-proj",
            "name": "Test Project",
            "description": "Test",
            "created_at": "2026-04-24T00:00:00Z",
            "max_turns": 100,
            "drain_turn": 80,
        },
        "stages": {
            "stage-1": {
                "stage_id": "stage-1",
                "name": "stage-1",
                "status": "active",
                "created_at": "2026-04-24T00:00:00Z",
                "node_ids": ["api.routes"],
            }
        },
        "nodes": {
            "api.routes": {
                "node_id": "api.routes",
                "stage_id": "stage-1",
                "type": "glue",
                "status": "near",
                "protocol_weight": "lean",
                "description": "API routes",
                "interface": {
                    "exports": [],
                    "assumptions": [],
                    "raises": [],
                    "guarantees": [],
                },
                "structural_deps": ["utils.serialization"],
                "assumption_deps": [],
                "test_contract": "TEST CONTRACT: api.routes\n\nAPI:\n- def build_app() -> Application\n\nBEHAVIOR:\n- Returns configured application",
                "committed_files": [],
                "tags": [],
                "view_spec": {},
                "metadata": {},
            },
            "utils.serialization": {
                "node_id": "utils.serialization",
                "stage_id": "stage-1",
                "type": "feature",
                "status": "grounded",
                "protocol_weight": "lean",
                "description": "Serialization helpers",
                "interface": {
                    "exports": ["def dumps(obj) -> str"],
                    "assumptions": [],
                    "raises": [],
                    "guarantees": [],
                },
                "structural_deps": [],
                "assumption_deps": [],
                "committed_files": ["utils/serialization.py"],
                "tags": [],
                "view_spec": {},
                "metadata": {
                    "first_export_preview": "def dumps(obj) -> str",
                    "plumbing_summary": "JSON serialization",
                    "tokens": 50,
                    "lines": 20,
                    "content_hash": "abc123",
                },
            },
        },
        "dependencies": [
            {
                "from_node": "api.routes",
                "to_node": "utils.serialization",
                "type": "structural",
                "verified": True,
            }
        ],
        "graveyard": [],
    }

    view = builder_view(doc, "api.routes")

    # Verify expected top-level keys
    expected_keys = {
        "role",
        "target",
        "structural_deps",
        "assumption_deps",
        "test_contract",
    }
    actual_keys = set(view.keys())

    for key in expected_keys:
        assert key in actual_keys, (
            f"Missing key '{key}' in builder view. Got: {actual_keys}"
        )

    # Verify test_contract specifically
    assert "test_contract" in view
    assert "build_app" in view["test_contract"]

    # Verify structural deps are included
    assert "utils.serialization" in view["structural_deps"]

    print("✓ builder_view has correct structure")
    print(f"✓ Keys present: {sorted(actual_keys)}")


def test_mode_b_validation_requires_test_contract():
    """
    Test that _parse_submit_pr rejects Mode B submissions without test_contract.

    Mode B = original node exports were empty (from ctx).
    The validation should return SUSPENDED with error message when test_contract
    is missing in Mode B.
    """
    # Create a minimal AgentLoop for testing
    from unittest.mock import MagicMock

    loop = AgentLoop.__new__(AgentLoop)
    loop._tools = MagicMock()
    loop._tools.get_usage.return_value = {}
    loop._tools.get_test_runs.return_value = []
    loop._log_session_event = MagicMock()

    # Test with test_author view format (interface at top level)
    ctx_mode_b_test_author = {
        "interface": {
            "exports": [],  # Empty = Mode B
        },
    }

    args_no_contract = json.dumps(
        {
            "status": "provisional",
            "interface": {
                "exports": [
                    "class BaseError(Exception): def __init__(self, code: str, message: str)"
                ],
                "assumptions": [],
            },
            # test_contract is missing!
        }
    )

    result = loop._parse_submit_pr(
        arguments=args_no_contract,
        node_id="utils.errors",
        role="test_author",
        iterations_used=5,
        session_id="test-session",
        ctx=ctx_mode_b_test_author,
    )

    # Should be SUSPENDED due to missing test_contract
    assert result.status == NodeStatus.SUSPENDED, (
        f"Expected SUSPENDED for missing test_contract in Mode B (test_author view), got {result.status}"
    )
    assert result.suspension_reason is not None
    assert "test_contract REQUIRED" in result.suspension_reason.detail, (
        f"Expected test_contract error message, got: {result.suspension_reason.detail}"
    )
    print(
        "✓ Mode B validation rejects submission without test_contract (test_author view)"
    )

    # Also test with builder view format (target.interface.exports)
    ctx_mode_b_builder = {
        "target": {
            "node_id": "utils.errors",
            "interface": {
                "exports": [],  # Empty = Mode B
            },
        }
    }

    # Test submission WITHOUT test_contract in Mode B
    args_no_contract = json.dumps(
        {
            "status": "provisional",
            "interface": {
                "exports": [
                    "class BaseError(Exception): def __init__(self, code: str, message: str)"
                ],
                "assumptions": [],
            },
            # test_contract is missing!
        }
    )

    result = loop._parse_submit_pr(
        arguments=args_no_contract,
        node_id="utils.errors",
        role="test_author",
        iterations_used=5,
        session_id="test-session",
        ctx=ctx_mode_b_builder,
    )

    # Should be SUSPENDED due to missing test_contract
    assert result.status == NodeStatus.SUSPENDED, (
        f"Expected SUSPENDED for missing test_contract in Mode B (builder view), got {result.status}"
    )
    assert result.suspension_reason is not None
    assert "test_contract REQUIRED" in result.suspension_reason.detail, (
        f"Expected test_contract error message, got: {result.suspension_reason.detail}"
    )
    print("✓ Mode B validation rejects submission without test_contract (builder view)")

    # Test submission WITH test_contract in Mode B (should succeed)
    args_with_contract = json.dumps(
        {
            "status": "provisional",
            "interface": {
                "exports": [
                    "class BaseError(Exception): def __init__(self, code: str, message: str)"
                ],
                "assumptions": [],
            },
            "test_contract": "TEST CONTRACT: utils.errors\n\nAPI:\n- class BaseError(Exception)\n\nBEHAVIOR:\n- Root exception\n\nEDGES:\n- None\n\nCOERCION:\n- None",
        }
    )

    result2 = loop._parse_submit_pr(
        arguments=args_with_contract,
        node_id="utils.errors",
        role="test_author",
        iterations_used=5,
        session_id="test-session",
        ctx=ctx_mode_b_test_author,
    )

    # Should be PROVISIONAL (not suspended)
    assert result2.status == NodeStatus.PROVISIONAL, (
        f"Expected PROVISIONAL with test_contract, got {result2.status}"
    )
    assert result2.test_contract is not None
    assert "TEST CONTRACT: utils.errors" in result2.test_contract
    print("✓ Mode B validation accepts submission with test_contract")

    # Test Mode A (exports already defined) - test_contract optional
    ctx_mode_a = {
        "target": {
            "node_id": "utils.validation",
            "interface": {
                "exports": [
                    "def validate_email(email: str) -> bool"
                ],  # Not empty = Mode A
            },
        }
    }

    args_mode_a = json.dumps(
        {
            "status": "grounded",
            "interface": {
                "exports": ["def validate_email(email: str) -> bool"],
                "assumptions": [],
            },
            # No test_contract - should be OK in Mode A
        }
    )

    # Use test_author view format for Mode A test too
    ctx_mode_a_test_author = {
        "interface": {
            "exports": ["def validate_email(email: str) -> bool"],
        },
    }

    result3 = loop._parse_submit_pr(
        arguments=args_mode_a,
        node_id="utils.validation",
        role="test_author",
        iterations_used=3,
        session_id="test-session",
        ctx=ctx_mode_a_test_author,
    )

    # Should be GROUNDED (not suspended)
    assert result3.status == NodeStatus.GROUNDED, (
        f"Expected GROUNDED for Mode A without test_contract, got {result3.status}"
    )
    print("✓ Mode A validation allows submission without test_contract")


def test_end_to_end_test_author_to_builder_flow():
    """
    End-to-end test: Simulates complete flow from test_author defining contract
    to builder receiving it.

    Flow:
    1. test_author submits with test_contract in Mode B
    2. Validation passes
    3. test_contract stored on node
    4. builder_view includes test_contract for builder
    """
    from unittest.mock import MagicMock

    # Step 1: Simulate test_author Mode B submission WITH test_contract
    loop = AgentLoop.__new__(AgentLoop)
    loop._tools = MagicMock()
    loop._tools.get_usage.return_value = {}
    loop._tools.get_test_runs.return_value = []
    loop._log_session_event = MagicMock()

    ctx_mode_b = {
        "target": {
            "node_id": "utils.errors",
            "interface": {"exports": []},  # Empty = Mode B
        }
    }

    test_contract_content = """TEST CONTRACT: utils.errors

API:
- class BaseError(Exception): def __init__(self, code: str, message: str)
- class ValidationError(BaseError): def __init__(self, message: str) -> code='VAL001'

BEHAVIOR:
- BaseError is root exception with code and message attributes
- ValidationError has predefined code 'VAL001'

EDGES:
- None

COERCION:
- None"""

    args = json.dumps(
        {
            "status": "provisional",
            "interface": {
                "exports": [
                    "class BaseError(Exception): def __init__(self, code: str, message: str)",
                    "class ValidationError(BaseError): def __init__(self, message: str) -> code='VAL001'",
                ],
                "assumptions": [],
            },
            "test_contract": test_contract_content,
        }
    )

    # Parse submission
    result = loop._parse_submit_pr(
        arguments=args,
        node_id="utils.errors",
        role="test_author",
        iterations_used=5,
        session_id="test-session",
        ctx=ctx_mode_b,
    )

    # Validation should pass
    assert result.status == NodeStatus.PROVISIONAL
    assert result.test_contract == test_contract_content
    print("✓ Step 1: test_author submission with test_contract validated")

    # Step 2: Simulate storing test_contract on node (what harness would do)
    doc = {
        "project": {
            "project_id": "test-proj",
            "name": "Test Project",
            "description": "Test",
            "created_at": "2026-04-24T00:00:00Z",
            "max_turns": 100,
            "drain_turn": 80,
        },
        "stages": {
            "stage-1": {
                "stage_id": "stage-1",
                "name": "stage-1",
                "status": "active",
                "created_at": "2026-04-24T00:00:00Z",
                "node_ids": ["utils.errors"],
            }
        },
        "nodes": {
            "utils.errors": {
                "node_id": "utils.errors",
                "stage_id": "stage-1",
                "type": "feature",
                "status": "provisional",
                "protocol_weight": "lean",
                "description": "Standard exception hierarchy",
                "interface": {
                    "exports": [
                        "class BaseError(Exception): def __init__(self, code: str, message: str)",
                        "class ValidationError(BaseError): def __init__(self, message: str) -> code='VAL001'",
                    ],
                    "assumptions": [],
                    "raises": [],
                    "guarantees": [],
                },
                "structural_deps": [],
                "assumption_deps": [],
                # test_contract stored from test_author submission
                "test_contract": result.test_contract,
                "committed_files": ["utils.errors.test"],
                "tags": [],
                "view_spec": {},
                "metadata": {},
            }
        },
        "dependencies": [],
        "graveyard": [],
    }
    print("✓ Step 2: test_contract stored on node in master.json")

    # Step 3: Builder view includes test_contract
    builder_ctx = builder_view(doc, "utils.errors")

    assert "test_contract" in builder_ctx
    assert builder_ctx["test_contract"] == test_contract_content
    print("✓ Step 3: builder_view includes test_contract")
    print(f"✓ Builder receives {len(builder_ctx['test_contract'])} char contract")

    # Step 4: Verify builder can see the contract details
    tc = builder_ctx["test_contract"]
    assert "BaseError" in tc
    assert "ValidationError" in tc
    assert "code='VAL001'" in tc
    print("✓ Step 4: contract details visible to builder")

    print("\n✓ End-to-end flow verified: test_author -> harness -> builder")


def test_test_contract_persisted_via_apply_return():
    """
    Test that test_contract is actually persisted to the node when test_author
    submits via the full _apply_return path (not just _parse_submit_pr).

    This catches the bug where _write_test_author_files had an early return
    that bypassed _build_node_updates where test_contract would be persisted.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from a7_rt_core.core.models import (
        ManagerState,
        NodeStatus,
        SuspensionReason,
        SuspensionType,
    )
    from harness.control import ControlMixin

    # Create a temp directory for the test session
    temp_dir = tempfile.mkdtemp()
    session_path = Path(temp_dir) / "test_session"

    try:
        # Initialize a real session
        from a7_rt_core.core.models import create_project
        from a7_rt_core.storage.repository import Repository

        project = create_project(name="Test Project", description="Test")
        repo = Repository.init(session_path, project)

        # Create a test feature node
        from a7_rt_core.core.models import NodeType, dispatch_node

        stage = repo.create_stage("stage-1")

        node = dispatch_node(
            node_id="test.feature",
            stage_id=stage.stage_id,
            type=NodeType.FEATURE,
            description="A test feature",
            interface={"exports": [], "assumptions": []},
        )
        repo.add_node(node)

        # Create a SubagentReturn as if from test_author with test_contract
        result = SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            files={"test.feature.test": "def test_feature(): pass"},
            content=None,
            edits=[],
            interface_update=None,
            file_tags=[],
            escalate=False,
            suspension_reason=None,
            iterations_used=3,
            tool_usage={},
            role="test_author",
            session_id="test-session-123",
            pr_note="Test PR note",
            test_contract="TEST CONTRACT: test.feature\nAPI: def feature() -> None\nEDGES: None",
            test_runs=[],
        )

        # Create minimal harness to call _apply_return (need ApplyMixin + UtilsMixin)
        from harness.apply import ApplyMixin
        from harness.utils import UtilsMixin

        class MinimalHarness(ApplyMixin, UtilsMixin):
            def __init__(self, repo):
                self.repo = repo
                self._current_shadow = None
                self.git_root = None

        control = MinimalHarness(repo)
        state = ManagerState(
            turn=1, max_turns=50, drain_turn=40, current_stage_id=stage.stage_id
        )

        # Call _apply_return which should persist test_contract
        control._apply_return("test.feature", result, NodeStatus.PROVISIONAL, state)

        # Verify test_contract was persisted to the node
        doc = repo._load()
        node = doc["nodes"]["test.feature"]

        assert "test_contract" in node, "test_contract should be persisted to node"
        assert node["test_contract"] == result.test_contract, (
            f"test_contract mismatch: expected {result.test_contract!r}, got {node['test_contract']!r}"
        )

        print("✓ test_contract persisted via _apply_return path")
        print(f"✓ Contract content: {node['test_contract'][:50]}...")

    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    test_test_contract_propagation()
    test_test_contract_absent_when_not_set()
    test_builder_view_structure()
    test_mode_b_validation_requires_test_contract()
    test_end_to_end_test_author_to_builder_flow()
    print("\n✓ All test_contract propagation tests passed")
