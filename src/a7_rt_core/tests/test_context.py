"""
Extracted inline tests for context.py.
Run standalone: python tests/test_context.py
"""

import json
import os
import sys

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any

from a7_rt_core.context.core import (
    BudgetExceeded,
    _count_tokens,
    analyst_view,
    assemble_test_author_view,
    builder_view,
    manager_view,
)

MasterDoc = dict[str, Any]

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

_FIXTURE_DOC: MasterDoc = {
    "project": {
        "project_id": "proj-001",
        "name": "auth-system",
        "description": "JWT auth with Redis token store",
        "max_turns": 25,
        "drain_turn": 20,
        "stage_ids": ["stage-001", "stage-002"],
    },
    "stages": {
        "stage-001": {
            "stage_id": "stage-001",
            "project_id": "proj-001",
            "name": "auth-core",
            "status": "sealed",
            "node_ids": ["node-jwt"],
            "sealed_at": "2026-04-06T00:00:00Z",
            "summary": "JWT validation complete",
            "exported_interfaces": {
                "node-jwt": {
                    "exports": ["verify_token(token: str) -> bool"],
                    "assumptions": [],
                }
            },
        },
        "stage-002": {
            "stage_id": "stage-002",
            "project_id": "proj-001",
            "name": "cache-layer",
            "status": "active",
            "node_ids": ["node-cache", "node-store", "node-glue", "node-blocked"],
        },
    },
    "nodes": {
        # Sealed stage — grounded, available as dep
        "node-jwt": {
            "node_id": "node-jwt",
            "stage_id": "stage-001",
            "type": "feature",
            "status": "grounded",
            "description": "JWT validation middleware — verify and decode tokens",
            "protocol_weight": "lean",
            "interface": {
                "exports": ["verify_token(token: str) -> bool"],
                "assumptions": ["jwt_decode returns dict with exp key"],
            },
            "structural_deps": [],
            "assumption_deps": [],
            "suspension_reason": None,
            "content_file": "content/node-jwt",
        },
        # Active stage nodes
        "node-cache": {
            "node_id": "node-cache",
            "stage_id": "stage-002",
            "type": "feature",
            "status": "grounded",
            "description": "Redis cache wrapper with get/set",
            "protocol_weight": "lean",
            "interface": {
                "exports": [
                    "get(key: str) -> str | None",
                    "set(key: str, value: str) -> None",
                ],
                "assumptions": ["Redis available on localhost:6379"],
            },
            "structural_deps": [],
            "assumption_deps": [],
            "suspension_reason": None,
            "content_file": "content/node-cache",
        },
        "node-store": {
            "node_id": "node-store",
            "stage_id": "stage-002",
            "type": "feature",
            "status": "provisional",
            "description": "Token store backed by Redis cache",
            "protocol_weight": "lean",
            "interface": {
                "exports": [
                    "store_token(token: str) -> None",
                    "get_token(key: str) -> str",
                ],
                "assumptions": ["cache TTL is 3600s"],
            },
            "structural_deps": ["node-cache"],
            "assumption_deps": ["node-jwt"],
            "suspension_reason": None,
            "content_file": "content/node-store",
        },
        "node-glue": {
            "node_id": "node-glue",
            "stage_id": "stage-002",
            "type": "glue",
            "status": "near",
            "description": "Glue: wire JWT validator to token store pipeline",
            "protocol_weight": "lean",
            "interface": {
                "exports": ["handle_auth(req) -> Response"],
                "assumptions": [],
            },
            "structural_deps": ["node-cache", "node-store"],
            "assumption_deps": [],
            "suspension_reason": None,
            "content_file": None,
        },
        "node-blocked": {
            "node_id": "node-blocked",
            "stage_id": "stage-002",
            "type": "feature",
            "status": "near",
            "description": "Rate limiter — depends on glue (not yet ready)",
            "protocol_weight": "lean",
            "interface": {"exports": [], "assumptions": []},
            "structural_deps": ["node-glue"],  # glue is near → blocked
            "assumption_deps": [],
            "suspension_reason": None,
            "content_file": None,
        },
    },
    "dependencies": [
        {
            "from_node": "node-store",
            "to_node": "node-cache",
            "type": "structural",
            "verified": True,
        },
        {
            "from_node": "node-store",
            "to_node": "node-jwt",
            "type": "assumption",
            "verified": True,
        },
        {
            "from_node": "node-glue",
            "to_node": "node-cache",
            "type": "structural",
            "verified": True,
        },
        {
            "from_node": "node-glue",
            "to_node": "node-store",
            "type": "structural",
            "verified": True,
        },
        {
            "from_node": "node-blocked",
            "to_node": "node-glue",
            "type": "structural",
            "verified": True,
        },
    ],
    "graveyard": [
        {
            "node_id": "node-pool",
            "scope": "global",
            "reason": "Redis connection pool implementation failed — timeout assumption invalid",
            "stage_id": "stage-001",
        },
        {
            "node_id": "node-parse",
            "scope": "local",
            "reason": "JWT token parsing edge case — local dead end in stage-001",
            "stage_id": "stage-001",
        },
    ],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_context() -> None:
    """
    End-to-end test: verify each view returns the correct shape and
    enforces the role access matrix (especially: manager/test_author never
    receive content_file or source code).
    """
    ok = "\033[32mOK\033[0m"
    fail = "\033[31mFAIL\033[0m"
    errors: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            print(f"  [{ok}] {label}")
        else:
            print(f"  [{fail}] {label}")
            errors.append(label)

    def no_content_file(obj: Any) -> bool:
        """True if 'content_file' does not appear anywhere in the JSON representation."""
        return "content_file" not in json.dumps(obj)

    doc = _FIXTURE_DOC

    # ── manager_view ─────────────────────────────────────────────────────────

    view = manager_view(doc, budget_tokens=10_000)

    check(
        "manager: active stage is stage-002",
        view["active_stage"]["stage_id"] == "stage-002",
    )
    check(
        "manager: all 4 active nodes present",
        set(view["nodes"]) == {"node-cache", "node-store", "node-glue", "node-blocked"},
    )
    check("manager: no content_file anywhere", no_content_file(view))
    check("manager: node-jwt (sealed stage) not in view", "node-jwt" not in view["nodes"])
    check(
        "manager: global tombstone present",
        any(e["node_id"] == "node-pool" for e in view["global_tombstones"]),
    )
    check(
        "manager: local tombstone absent",
        all(e["node_id"] != "node-parse" for e in view["global_tombstones"]),
    )
    check(
        "manager: sealed stage-001 present in sealed_stages",
        any(s["stage_id"] == "stage-001" for s in view["sealed_stages"]),
    )
    check(
        "manager: sealed stage summary present",
        any(s.get("summary") == "JWT validation complete" for s in view["sealed_stages"]),
    )
    check(
        "manager: sealed stage exported interfaces present",
        any("node-jwt" in s.get("exported_interfaces", {}) for s in view["sealed_stages"]),
    )
    check(
        "manager: sealed_stages has no content_file",
        no_content_file(view["sealed_stages"]),
    )
    check(
        "manager: active stage not in sealed_stages",
        all(s["stage_id"] != "stage-002" for s in view["sealed_stages"]),
    )
    check(
        "manager: node-glue in ready (cache grnd, store prov)",
        "node-glue" in view["ready"],
    )
    check(
        "manager: node-blocked NOT ready (glue is near)",
        "node-blocked" not in view["ready"],
    )
    check("manager: dep edges present", len(view["dependencies"]) > 0)
    check(
        "manager: cross-stage dep (node-store→node-jwt) NOT in stage deps",
        not any(
            d["from_node"] == "node-store" and d["to_node"] == "node-jwt"
            for d in view["dependencies"]
        ),
    )

    # Budget enforcement
    try:
        manager_view(doc, budget_tokens=1)
        check("manager: budget=1 raises BudgetExceeded", False)
    except BudgetExceeded as e:
        check("manager: budget=1 raises BudgetExceeded", True)
        check(
            "manager: BudgetExceeded carries actual/budget",
            e.actual > 0 and e.budget == 1,
        )

    # No active stage → graceful empty view
    no_active_doc = {
        **doc,
        "stages": {
            k: {**v, "status": "sealed", "sealed_at": "2026-04-06T00:00:00Z"}
            for k, v in doc["stages"].items()
        },
    }
    empty_view = manager_view(no_active_doc, budget_tokens=10_000)
    check(
        "manager: no active stage → active_stage=None",
        empty_view["active_stage"] is None,
    )

    # last_error surfaced on board
    view_with_err = manager_view(
        doc, budget_tokens=10_000, last_error="SEAL rejected: near nodes remain"
    )
    check(
        "manager: last_error passed through to view",
        view_with_err["last_error"] == "SEAL rejected: near nodes remain",
    )
    view_no_err = manager_view(doc, budget_tokens=10_000)
    check("manager: last_error=None when not passed", view_no_err["last_error"] is None)

    # resume_warning: in_flight non-empty + no pending_returns → warning emitted
    class _FakeState:
        current_stage_id = "stage-002"
        turn = 3
        manager_max_turns = 25
        drain_turn = 20
        in_flight = ["node-cache"]
        mode = type("M", (), {"value": "autonomous"})()
        planning = None
        human_input_queue = []

    view_resume = manager_view(
        doc,
        budget_tokens=10_000,
        state=_FakeState(),
        pending_returns={},
        lifecycle="resumed",
    )
    check(
        "manager: resume_warning non-null when in_flight+no pending_returns on resume",
        view_resume["resume_warning"] is not None and "node-cache" in view_resume["resume_warning"],
    )

    # resume_warning: absent when pending_returns covers in_flight
    class _ReturnStub:
        escalate = False

    view_resume_ok = manager_view(
        doc,
        budget_tokens=10_000,
        state=_FakeState(),
        pending_returns={"node-cache": _ReturnStub()},
        lifecycle="resumed",
    )
    check(
        "manager: resume_warning=None when pending_returns covers in_flight",
        view_resume_ok["resume_warning"] is None,
    )

    # ── manager_view: node summaries + enumerated targets ─────────────────────

    # Node with metadata — verify summary fields present
    doc_with_meta = json.loads(json.dumps(doc))  # deep copy
    doc_with_meta["nodes"]["node-cache"]["metadata"] = {
        "tokens": 150,
        "lines": 20,
        "content_hash": "abc123def456",
        "first_export_preview": "def get(key: str) -> str | None",
        "plumbing_summary": "Exports: get, set | Size: 20 lines",
        "computed_at": "2026-04-11T00:00:00Z",
    }
    view_meta = manager_view(doc_with_meta, budget_tokens=10_000)
    cache_summary = view_meta["nodes"]["node-cache"]
    check(
        "manager(summary): tokens present when metadata available",
        cache_summary.get("tokens") == 150,
    )
    check(
        "manager(summary): exports_preview present when metadata available",
        cache_summary.get("exports_preview") == "def get(key: str) -> str | None",
    )
    check("manager(summary): blocking_count present", "blocking_count" in cache_summary)
    # node-cache has no structural deps that aren't dispatchable
    check(
        "manager(summary): blocking_count correct for grounded node with no deps",
        cache_summary["blocking_count"] == 0,
    )

    # Node without metadata — verify None placeholders
    store_summary = view_meta["nodes"]["node-store"]
    check(
        "manager(summary): tokens is None when metadata absent",
        store_summary.get("tokens") is None,
    )
    check(
        "manager(summary): exports_preview is None when metadata absent",
        store_summary.get("exports_preview") is None,
    )

    # blocking_count: node-blocked depends on node-glue (which is near)
    blocked_summary = view_meta["nodes"]["node-blocked"]
    check(
        "manager(summary): blocking_count correct for blocked node",
        blocked_summary["blocking_count"] == 1,
    )
    check(
        "manager(summary): structural_dep_count present",
        blocked_summary["structural_dep_count"] == 1,
    )

    # Enumerated valid targets — prevents hallucination
    check(
        "manager(valid_targets): list present in view",
        "valid_dispatch_targets" in view_meta,
    )
    # valid_targets should include ready nodes (node-glue) and near/suspended nodes
    valid_targets = set(view_meta["valid_dispatch_targets"])
    check("manager(valid_targets): includes ready nodes", "node-glue" in valid_targets)
    # node-blocked is NOT a valid target because it's blocked (node-glue is near)
    check(
        "manager(valid_targets): excludes blocked near nodes",
        "node-blocked" not in valid_targets,
    )
    # Should NOT include already-grounded or provisional nodes
    check(
        "manager(valid_targets): excludes grounded nodes",
        "node-cache" not in valid_targets,
    )
    check(
        "manager(valid_targets): includes provisional nodes with grounded deps",
        "node-store" in valid_targets,
    )

    # Test valid_targets with in-flight and pending nodes
    class _FakeStateInFlight:
        current_stage_id = "stage-002"
        turn = 3
        manager_max_turns = 25
        drain_turn = 20
        in_flight = ["node-blocked"]  # in-flight should be valid target
        mode = type("M", (), {"value": "autonomous"})()
        planning = None
        human_input_queue = []

    class _PendingReturnStub:
        escalate = False

    view_inflight = manager_view(
        doc_with_meta,
        budget_tokens=10_000,
        state=_FakeStateInFlight(),
        pending_returns={"node-store": _PendingReturnStub()},  # pending should be valid target
        lifecycle="new",
    )
    inflight_targets = set(view_inflight["valid_dispatch_targets"])
    check(
        "manager(valid_targets): includes in-flight nodes",
        "node-blocked" in inflight_targets,
    )
    check(
        "manager(valid_targets): includes pending_return nodes",
        "node-store" in inflight_targets,
    )

    # Token budget breakdown
    check("manager(token_budget): budget info present", "token_budget" in view_meta)
    check(
        "manager(token_budget): allocated field present",
        view_meta["token_budget"]["allocated"] == 10000,
    )
    check(
        "manager(token_budget): consumed field present and > 0",
        view_meta["token_budget"]["consumed"] > 0,
    )
    check(
        "manager(token_budget): nodes_count correct",
        view_meta["token_budget"]["nodes_count"] == 4,
    )
    check(
        "manager(token_budget): ready_count correct",
        view_meta["token_budget"]["ready_count"] == 2,
    )  # node-store (provisional) and node-glue (near) both ready

    # ── builder_view ──────────────────────────────────────────────────────────

    bv = builder_view(doc, "node-glue")

    check("builder: role=builder", bv["role"] == "builder")
    check("builder: target is node-glue", bv["target"]["node_id"] == "node-glue")
    check(
        "builder: structural deps present",
        set(bv["structural_deps"]) == {"node-cache", "node-store"},
    )
    check("builder: no assumption deps for node-glue", bv["assumption_deps"] == {})
    check(
        "builder: structural deps include content_file",
        all("content_file" in n for n in bv["structural_deps"].values()),
    )
    check(
        "builder: global tombstone in results",
        any(e["node_id"] == "node-pool" for e in bv["tombstones"]),
    )

    # Depth-2 ancestor: node-jwt (via node-store's assumption dep)
    all_ancestor_ids = set(bv.get("ancestor_interfaces", {})) | set(
        bv.get("ancestor_summaries", {})
    )
    check(
        "builder: node-jwt appears as ancestor (depth 2)",
        "node-jwt" in all_ancestor_ids,
    )
    check(
        "builder: ancestor_interfaces have no content_file",
        no_content_file(bv.get("ancestor_interfaces", {})),
    )
    check(
        "builder: ancestor_summaries are strings",
        all(isinstance(v, str) for v in bv.get("ancestor_summaries", {}).values()),
    )

    # node-store: local tombstone (JWT keyword overlap with "node-parse: JWT edge case in parsing")
    bv_store = builder_view(doc, "node-store")
    check(
        "builder(node-store): local tombstone included via keyword overlap",
        any(e["node_id"] == "node-parse" for e in bv_store["tombstones"]),
    )

    # node-cache: no keyword overlap with "JWT edge case" → local tombstone excluded
    bv_cache = builder_view(doc, "node-cache")
    check(
        "builder(node-cache): local tombstone excluded (no overlap)",
        all(e["node_id"] != "node-parse" for e in bv_cache["tombstones"]),
    )

    # ── test_author_view ──────────────────────────────────────────────────────

    tv = assemble_test_author_view(doc, "node-glue")

    check("test_author: role=test_author", tv["role"] == "test_author")
    check("test_author: node_id correct", tv["node_id"] == "node-glue")
    check("test_author: description present", tv["description"] is not None)
    check("test_author: interface present", "exports" in tv["interface"])
    check("test_author: no content_file", no_content_file(tv))
    check(
        "test_author: structural dep interfaces present",
        set(tv["structural_dep_interfaces"]) == {"node-cache", "node-store"},
    )
    check(
        "test_author: dep interfaces have no content_file",
        no_content_file(tv["structural_dep_interfaces"]),
    )
    check("test_author: no tombstones key", "tombstones" not in tv)
    check(
        "test_author: no 'source' or 'content' keys",
        "source" not in tv and "content" not in tv,
    )

    # ── analyst_view ──────────────────────────────────────────────────────────

    av = analyst_view(
        doc,
        "Does node-glue correctly wire cache and store?",
        ["node-glue", "node-store"],
    )

    check("analyst: role=analyst", av["role"] == "analyst")
    check("analyst: query preserved", "node-glue" in av["query"])
    check("analyst: both nodes present", set(av["nodes"]) == {"node-glue", "node-store"})
    check(
        "analyst: full node dicts (content_file accessible)",
        "content_file" in av["nodes"]["node-store"],
    )
    check(
        "analyst: dep subgraph contains glue→store edge",
        any(
            d["from_node"] == "node-glue" and d["to_node"] == "node-store"
            for d in av["dependencies"]
        ),
    )
    check("analyst: all tombstones present (local + global)", len(av["tombstones"]) == 2)

    # ── Token counting ────────────────────────────────────────────────────────

    tokens = _count_tokens({"hello": "world", "count": 42})
    check("token_count: non-zero for non-empty object", tokens > 0)
    check("token_count: empty object → 1 token ('{}')", _count_tokens({}) == 1)
    # 1.3x safety margin: result must exceed raw word count
    _raw_words = len(json.dumps({"hello": "world", "count": 42}).split())
    check(
        "token_count: 1.3x margin applied (result > raw word count)",
        tokens > _raw_words,
    )

    # ── Summary ───────────────────────────────────────────────────────────────

    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\033[32mAll context tests passed.\033[0m")


if __name__ == "__main__":
    print("Running context tests...")
    test_context()
