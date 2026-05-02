"""
Extracted inline tests for repository.py.
Run standalone: python tests/test_repository.py
"""

import json
import os
import sys
import tempfile

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from pathlib import Path

from a7_rt_core.core.models import (
    InterfaceContract,
    ManagerMode,
    ManagerState,
    NodeStatus,
    NodeType,
    StageStatus,
    SuspensionReason,
    SuspensionType,
    create_project,
    dispatch_node,
)
from a7_rt_core.storage.repository import (
    InvariantViolation,
    Repository,
    RepositoryError,
)


def _test_repository() -> None:
    """
    End-to-end test: create project → add stage → add nodes → transitions →
    manager lifecycle → seal stage → verify directory state.
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

    def expect_raise(label: str, exc_type: type, fn) -> None:
        try:
            fn()
            print(f"  [{fail}] {label}  (no exception raised)")
            errors.append(label)
        except exc_type:
            print(f"  [{ok}] {label}")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # ── 1. Init ──────────────────────────────────────────────────────────
        project = create_project("auth-system", "JWT auth with token store")
        repo = Repository.init(root, project)

        check("master.json created", (root / "master.json").exists())
        check("events.jsonl created", (root / "events.jsonl").exists())
        check("content/ created", (root / "content").is_dir())
        expect_raise(
            "double-init rejected",
            RepositoryError,
            lambda: Repository.init(root, project),
        )

        # ── 2. Create stage ───────────────────────────────────────────────────
        stage = repo.create_stage("auth-core")
        check("stage active", stage.status == StageStatus.ACTIVE)
        doc = repo._load()
        check("stage in master", stage.stage_id in doc["stages"])
        check(
            "stage in project.stage_ids", stage.stage_id in doc["project"]["stage_ids"]
        )

        # ── 3. Single-active-stage invariant ─────────────────────────────────
        expect_raise(
            "second active stage rejected",
            InvariantViolation,
            lambda: repo.create_stage("second-stage"),
        )

        # ── 4. Add nodes ──────────────────────────────────────────────────────
        n1 = dispatch_node(
            "auth-001", stage.stage_id, NodeType.FEATURE, "JWT middleware"
        )
        n2 = dispatch_node("auth-002", stage.stage_id, NodeType.FEATURE, "Token store")
        repo.add_node(n1)
        repo.add_node(n2)

        doc = repo._load()
        check("auth-001 in master", "auth-001" in doc["nodes"])
        check("auth-002 in master", "auth-002" in doc["nodes"])
        check(
            "nodes in stage.node_ids",
            "auth-001" in doc["stages"][stage.stage_id]["node_ids"],
        )

        expect_raise(
            "duplicate node rejected",
            RepositoryError,
            lambda: repo.add_node(n1),
        )

        # ── 5. Valid status transitions ───────────────────────────────────────
        repo.update_node("auth-001", status=NodeStatus.PROVISIONAL)
        repo.update_node("auth-001", status=NodeStatus.GROUNDED)
        repo.update_node("auth-002", status=NodeStatus.PROVISIONAL)
        repo.update_node("auth-002", status=NodeStatus.GROUNDED)

        doc = repo._load()
        check("auth-001 grounded", doc["nodes"]["auth-001"]["status"] == "grounded")
        check("auth-002 grounded", doc["nodes"]["auth-002"]["status"] == "grounded")

        # ── 6. Invalid transition rejected ────────────────────────────────────
        expect_raise(
            "grounded→provisional rejected",
            ValueError,
            lambda: repo.update_node("auth-001", status=NodeStatus.PROVISIONAL),
        )

        # ── 7. Board view — no content_file ───────────────────────────────────
        view = repo.get_board_view()
        check(
            "active stage in view", view["active_stage"]["stage_id"] == stage.stage_id
        )
        check("auth-001 in view", "auth-001" in view["nodes"])
        check(
            "content_file absent from view",
            all("content_file" not in n for n in view["nodes"].values()),
        )

        # ── 8. Append event ───────────────────────────────────────────────────
        repo.append_event(
            {
                "turn": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": "manager",
                "action": "dispatch",
                "target": "auth-001",
                "detail": "Dispatched builder [lean]",
            }
        )
        lines = (root / "events.jsonl").read_text().strip().splitlines()
        check("event appended", len(lines) == 1)
        check("event target correct", json.loads(lines[0])["target"] == "auth-001")

        # ── 9a. Manager checkpoint ───────────────────────────────────────────
        os_state = ManagerState(current_stage_id=stage.stage_id, turn=5)
        repo.manager_checkpoint(os_state)
        check("manager.json created", repo.manager_alive)
        loaded = repo.load_manager_state()
        check("turn persisted", loaded is not None and loaded.turn == 5)
        check(
            "checkpoint_at stamped",
            loaded is not None and loaded.checkpoint_at is not None,
        )

        # ── 9b. Manager kill (idempotent) ────────────────────────────────────
        repo.manager_kill()
        check("manager.json removed", not repo.manager_alive)
        repo.manager_kill()  # idempotent — must not raise
        check("double kill idempotent", not repo.manager_alive)

        # Dead manager cannot checkpoint
        expect_raise(
            "checkpoint dead manager rejected",
            InvariantViolation,
            lambda: repo.manager_checkpoint(
                ManagerState(
                    current_stage_id=stage.stage_id,
                    turn=25,
                    max_turns=25,
                    mode=ManagerMode.DEAD,
                )
            ),
        )

        # ── 9c. Manager seal ─────────────────────────────────────────────────
        os_state2 = ManagerState(current_stage_id=stage.stage_id, turn=5)
        repo.manager_seal(os_state2)
        check("manager dead after seal", not repo.manager_alive)
        lines = (root / "events.jsonl").read_text().strip().splitlines()
        check("death event appended", len(lines) == 2)
        death_evt = json.loads(lines[1])
        check("death event action", death_evt["action"] == "death")
        check("death event actor", death_evt["actor"] == "manager")

        # ── 10. Seal-stage gate: near nodes block seal ────────────────────────
        n3 = dispatch_node("auth-003", stage.stage_id, NodeType.FEATURE, "Rate limiter")
        repo.add_node(n3)  # status=near

        expect_raise(
            "seal rejected with near node",
            InvariantViolation,
            lambda: repo.seal_stage(stage.stage_id, "summary", {}),
        )

        # Resolve n3, then seal
        repo.update_node("auth-003", status=NodeStatus.PROVISIONAL)
        repo.update_node("auth-003", status=NodeStatus.GROUNDED)

        sealed = repo.seal_stage(
            stage.stage_id,
            "Auth core complete — JWT validation and token store grounded.",
            {
                "auth-001": InterfaceContract(
                    exports=["verify_token(token: str) -> bool"],
                    assumptions=["jwt_decode returns dict with exp key"],
                ),
                "auth-002": InterfaceContract(
                    exports=[
                        "store_token(token: str) -> None",
                        "get_token(key: str) -> str",
                    ],
                ),
            },
        )
        check("stage sealed", sealed.status == StageStatus.SEALED)
        check("sealed_at set", sealed.sealed_at is not None)
        check("summary persisted", sealed.summary is not None)

        doc = repo._load()
        check(
            "master reflects sealed",
            doc["stages"][stage.stage_id]["status"] == "sealed",
        )
        check(
            "exported interfaces stored",
            "auth-001" in doc["stages"][stage.stage_id]["exported_interfaces"],
        )

        # ── 10b. Seal-stage gate: suspended-NEAR nodes block seal ────────────
        # Create a fresh stage to test this in isolation
        stage_near = repo.create_stage("near-seal-test")
        n_near = dispatch_node(
            "near-001", stage_near.stage_id, NodeType.FEATURE, "Near-suspended node"
        )
        repo.add_node(n_near)
        repo.update_node(
            "near-001",
            status=NodeStatus.SUSPENDED,
            suspension_reason=SuspensionReason(
                type=SuspensionType.NEAR, detail="waiting on dep"
            ).model_dump(),
        )
        expect_raise(
            "seal rejected with suspended-NEAR node",
            InvariantViolation,
            lambda: repo.seal_stage(stage_near.stage_id, "summary", {}),
        )
        # Re-suspend as FAR — must cycle through near first (suspended→near→suspended)
        repo.update_node("near-001", status=NodeStatus.NEAR)
        repo.update_node(
            "near-001",
            status=NodeStatus.SUSPENDED,
            suspension_reason=SuspensionReason(
                type=SuspensionType.FAR, detail="external dep"
            ).model_dump(),
        )
        sealed_near = repo.seal_stage(
            stage_near.stage_id, "near-seal-test complete", {}
        )
        check(
            "stage seals after suspended-NEAR resolved to FAR",
            sealed_near.status == StageStatus.SEALED,
        )

        # ── 11. New stage possible after seal ─────────────────────────────────
        stage2 = repo.create_stage("cache-core")
        check("second stage created after seal", stage2.status == StageStatus.ACTIVE)

        # ── 12. Double-seal rejected ──────────────────────────────────────────
        expect_raise(
            "double-seal rejected",
            RepositoryError,
            lambda: repo.seal_stage(stage.stage_id, "again", {}),
        )

        # ── 13. Open existing repo ────────────────────────────────────────────
        repo2 = Repository.open(root)
        p = repo2.load_project()
        check("re-opened project name", p.name == "auth-system")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\033[32mAll repository tests passed.\033[0m")


if __name__ == "__main__":
    _test_repository()
