"""
Test PlanningState functionality for A7-RT Manager State Enhancement

Phase 5: Testing — PlanningState, ChecklistItem, UpdatePlanAction
"""

import json
import tempfile
from pathlib import Path

from a7_rt_core.core.models import (
    ChecklistItem,
    ManagerMode,
    ManagerState,
    PlanningState,
    UpdatePlanAction,
)
from a7_rt_core.llm.parser import build_action, parse_manager_action
from a7_rt_core.storage.repository import Repository

# ---------------------------------------------------------------------------
# Phase 5.1: PlanningState and ChecklistItem serialization
# ---------------------------------------------------------------------------


def test_checklist_item_defaults():
    """ChecklistItem generates id and status defaults."""
    item = ChecklistItem(text="Verify DI pattern")
    assert len(item.id) == 8  # UUID first 8 chars
    assert item.text == "Verify DI pattern"
    assert item.status == "pending"


def test_checklist_item_explicit():
    """ChecklistItem accepts explicit values."""
    item = ChecklistItem(id="c1", text="Check auth", status="satisfied")
    assert item.id == "c1"
    assert item.text == "Check auth"
    assert item.status == "satisfied"


def test_planning_state_defaults():
    """PlanningState has sensible defaults."""
    ps = PlanningState()
    assert ps.narrative is None
    assert ps.checklist == []
    assert ps.updated_at == 0


def test_planning_state_with_narrative():
    """PlanningState can carry just a narrative."""
    ps = PlanningState(narrative="Focus on auth module", updated_at=5)
    assert ps.narrative == "Focus on auth module"
    assert ps.checklist == []
    assert ps.updated_at == 5


def test_planning_state_with_checklist():
    """PlanningState can carry a checklist."""
    ps = PlanningState(
        narrative="Verify architecture",
        checklist=[
            ChecklistItem(text="All services use DI", status="satisfied"),
            ChecklistItem(text="No circular deps", status="pending"),
        ],
        updated_at=10,
    )
    assert ps.narrative == "Verify architecture"
    assert len(ps.checklist) == 2
    assert ps.checklist[0].text == "All services use DI"
    assert ps.checklist[0].status == "satisfied"
    assert ps.checklist[1].status == "pending"


def test_planning_state_serialization():
    """PlanningState serializes to dict correctly."""
    ps = PlanningState(
        narrative="Test",
        checklist=[ChecklistItem(text="Item 1")],
        updated_at=3,
    )
    data = ps.model_dump(mode="json")
    assert data["narrative"] == "Test"
    assert len(data["checklist"]) == 1
    assert data["checklist"][0]["text"] == "Item 1"
    assert data["checklist"][0]["status"] == "pending"
    assert data["updated_at"] == 3


# ---------------------------------------------------------------------------
# Phase 5.2: ManagerState with planning field
# ---------------------------------------------------------------------------


def test_manager_state_planning_field():
    """ManagerState uses planning field instead of stage_intent."""
    ps = PlanningState(narrative="JWT auth", updated_at=5)
    state = ManagerState(
        current_stage_id="auth",
        turn=5,
        planning=ps,
    )
    assert state.planning is not None
    assert state.planning.narrative == "JWT auth"


def test_manager_state_no_planning():
    """ManagerState works without planning."""
    state = ManagerState(
        current_stage_id="auth",
        turn=0,
        planning=None,
    )
    assert state.planning is None


def test_manager_state_optional_turn_limits():
    """max_turns and drain_turn are optional."""
    state = ManagerState(
        current_stage_id="auth",
        turn=10,
        manager_max_turns=None,  # Unlimited
        drain_turn=100,  # Soft pressure
    )
    assert state.manager_max_turns is None
    assert state.drain_turn == 100


# ---------------------------------------------------------------------------
# Phase 5.3: UPDATE_PLAN action parsing
# ---------------------------------------------------------------------------


def test_parse_update_plan_narrative_only():
    """Parse UPDATE_PLAN with just narrative."""
    raw = 'ACT: {"action": "UPDATE_PLAN", "narrative": "Focus on tests"}'
    action = parse_manager_action(raw)
    assert isinstance(action, UpdatePlanAction)
    assert action.narrative == "Focus on tests"
    assert action.checklist == []


def test_parse_update_plan_with_checklist():
    """Parse UPDATE_PLAN with checklist."""
    raw = """ACT: {
        "action": "UPDATE_PLAN",
        "narrative": "Verify architecture",
        "checklist": [
            {"text": "DI pattern used", "status": "satisfied"},
            {"text": "Error handling consistent", "status": "pending"}
        ]
    }"""
    action = parse_manager_action(raw)
    assert isinstance(action, UpdatePlanAction)
    assert action.narrative == "Verify architecture"
    assert len(action.checklist) == 2
    assert action.checklist[0].text == "DI pattern used"
    assert action.checklist[0].status == "satisfied"
    assert action.checklist[1].status == "pending"


def test_parse_update_plan_fenced():
    """Parse UPDATE_PLAN from fenced code block."""
    raw = """ACT:
```json
{"action": "UPDATE_PLAN", "narrative": "Refactor", "checklist": []}
```"""
    action = parse_manager_action(raw)
    assert isinstance(action, UpdatePlanAction)
    assert action.narrative == "Refactor"


# ---------------------------------------------------------------------------
# Phase 5.4: Migration from stage_intent to planning
# ---------------------------------------------------------------------------


def test_load_manager_state_migration():
    """Repository migrates old stage_intent to planning on load."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Repository(Path(tmpdir))

        # Create old-format manager.json with stage_intent
        old_data = {
            "current_stage_id": "auth",
            "turn": 5,
            "max_turns": 25,
            "drain_turn": 20,
            "mode": "autonomous",
            "in_flight": [],
            "human_input_queue": [],
            "stage_intent": "Old intent string",
        }

        manager_path = Path(tmpdir) / "manager.json"
        manager_path.write_text(json.dumps(old_data))

        # Load should migrate
        state = repo.load_manager_state()
        assert state is not None
        assert state.planning is not None
        assert state.planning.narrative == "Old intent string"
        assert state.planning.checklist == []
        assert state.planning.updated_at == 5


def test_load_manager_state_no_migration_needed():
    """Repository doesn't break when loading new format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Repository(Path(tmpdir))

        # Create new-format manager.json
        new_data = {
            "current_stage_id": "auth",
            "turn": 3,
            "planning": {
                "narrative": "New format",
                "checklist": [{"id": "c1", "text": "Item", "status": "pending"}],
                "updated_at": 3,
            },
        }

        manager_path = Path(tmpdir) / "manager.json"
        manager_path.write_text(json.dumps(new_data))

        state = repo.load_manager_state()
        assert state is not None
        assert state.planning is not None
        assert state.planning.narrative == "New format"
        assert len(state.planning.checklist) == 1


# ---------------------------------------------------------------------------
# Phase 5.5: Full lifecycle tests (update → board view → SEAL integration)
# ---------------------------------------------------------------------------


def test_manager_view_includes_planning():
    """manager_view includes planning state in output."""
    from a7_rt_core.context.core import manager_view

    ps = PlanningState(
        narrative="Test narrative",
        checklist=[ChecklistItem(text="Check 1")],
        updated_at=2,
    )
    state = ManagerState(
        current_stage_id="test",
        turn=2,
        planning=ps,
    )

    doc = {
        "project": {"max_turns": None, "drain_turn": 100},
        "stages": {},
        "nodes": {},
        "dependencies": [],
        "graveyard": [],
    }

    view = manager_view(doc, budget_tokens=10000, state=state)

    assert "planning" in view
    assert view["planning"]["narrative"] == "Test narrative"
    assert len(view["planning"]["checklist"]) == 1
    assert view["planning"]["checklist"][0]["text"] == "Check 1"


def test_manager_view_no_planning():
    """manager_view handles missing planning state."""
    from a7_rt_core.context.core import manager_view

    state = ManagerState(
        current_stage_id="test",
        turn=0,
        planning=None,
    )

    doc = {
        "project": {"max_turns": None, "drain_turn": 100},
        "stages": {},
        "nodes": {},
        "dependencies": [],
        "graveyard": [],
    }

    view = manager_view(doc, budget_tokens=10000, state=state)
    assert view["planning"] is None


def test_manager_view_recent_events():
    """manager_view includes recent strategic events."""
    from a7_rt_core.context.core import manager_view

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Repository(Path(tmpdir))

        # Add some events
        repo.append_event(
            {
                "turn": 1,
                "timestamp": "2024-01-01T00:00:00",
                "actor": "manager",
                "action": "dispatch",
                "target": "node-1",
                "detail": "dispatched",
            }
        )
        repo.append_event(
            {
                "turn": 2,
                "timestamp": "2024-01-01T00:00:01",
                "actor": "manager",
                "action": "commit",
                "target": "node-1",
                "detail": "grounded",
            }
        )

        state = ManagerState(current_stage_id="test", turn=2)

        doc = {
            "project": {"max_turns": None, "drain_turn": 100},
            "stages": {},
            "nodes": {},
            "dependencies": [],
            "graveyard": [],
        }

        view = manager_view(doc, budget_tokens=10000, repo=repo, state=state)

        assert "recent_events" in view
        assert len(view["recent_events"]) == 2
        assert view["recent_events"][0]["action"] == "commit"  # Most recent first


# ---------------------------------------------------------------------------
# Regression: /intent slash command
# ---------------------------------------------------------------------------


def test_intent_command_preserved():
    """Verify the /intent command structure is maintained."""
    # This is a smoke test that the harness methods exist
    # Check the _drain_events method handles /intent
    import inspect

    from a7_rt_core.harness.core import Harness

    source = inspect.getsource(Harness._drain_events)
    assert "/intent" in source


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    tests = [
        test_checklist_item_defaults,
        test_checklist_item_explicit,
        test_planning_state_defaults,
        test_planning_state_with_narrative,
        test_planning_state_with_checklist,
        test_planning_state_serialization,
        test_manager_state_planning_field,
        test_manager_state_no_planning,
        test_manager_state_optional_turn_limits,
        test_parse_update_plan_narrative_only,
        test_parse_update_plan_with_checklist,
        test_parse_update_plan_fenced,
        test_load_manager_state_migration,
        test_load_manager_state_no_migration_needed,
        test_manager_view_includes_planning,
        test_manager_view_no_planning,
        test_manager_view_recent_events,
        test_intent_command_preserved,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
