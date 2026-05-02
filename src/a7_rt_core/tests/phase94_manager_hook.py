"""
Phase 9.4 Test: Builder Retry with Analyst Context and Chronicle Hints

Manager hook that orchestrates:
1. Dispatch builder for auth_handler (will fail hard-path test)
2. Dispatch analyst to examine failure (findings -> metadata.analyst_findings)
3. Redispatch builder (retry) - verify context includes analyst_findings

Per Phase 6.5 spec:
- exploration_hints: previous_attempts, files_written only
- analyst_findings: separate from exploration_hints, visible via node metadata
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (
    CommitAction,
    DispatchAction,
    HaltAction,
    ManagerAction,
    NodeStatus,
    RedispatchAction,
    SealAction,
    UpdatePlanAction,
    ValidateAction,
)


def phase94_manager_hook(board: dict, state) -> ManagerAction:
    """
    Orchestrate phase 9.4 test workflow.

    Flow:
    - DISPATCH builder auth_handler
    - COMMIT builder result (status=provisional)
    - DISPATCH analyst auth_handler scope=node
    - COMMIT analyst findings (status preserved, findings in metadata)
    - REDISPATCH builder auth_handler
    - Verify retry context has analyst_findings
    """
    turn = state.turn
    pending_returns = board.get("pending_returns", {})
    pending_commits = board.get("pending_commits", {})
    nodes = board.get("nodes", {})

    # Get auth_handler status
    handler = nodes.get("auth_handler", {})
    handler_status = handler.get("status", "near")

    # Use board planning state to track flow (persists across turns)
    planning = board.get("planning") or {}
    narrative = planning.get("narrative", "")

    # Parse flow state from narrative (simple comma-separated flags)
    flow_flags = set(narrative.split(",")) if narrative else set()

    builder_done = "builder_done" in flow_flags
    analyst_done = "analyst_done" in flow_flags
    redispatch_done = "redispatch_done" in flow_flags

    # Helper to update planning state
    def update_plan(flags_to_add):
        new_flags = flow_flags | flags_to_add
        return UpdatePlanAction(
            narrative=",".join(sorted(new_flags)),
            checklist=[{"text": "Phase 9.4 retry test", "status": "pending"}],
        )

    # Log current state
    print(
        f"[PHASE94] Turn {turn}: status={handler_status}, "
        f"flags={flow_flags}, pending={list(pending_returns.keys())}, "
        f"commits={list(pending_commits.keys())}",
        file=sys.stderr,
    )

    # === PRIORITY 1: Handle pending returns ===
    if "auth_handler" in pending_returns:
        print(f"[PHASE94] Turn {turn}: VALIDATE auth_handler", file=sys.stderr)
        return ValidateAction(node_id="auth_handler")

    # === PRIORITY 2: Handle pending commits ===
    if "auth_handler" in pending_commits:
        commit_info = pending_commits["auth_handler"]
        role = commit_info.get("role", "builder")

        print(
            f"[PHASE94] Turn {turn}: COMMIT auth_handler (role={role})", file=sys.stderr
        )

        # Determine status to commit
        if role == "analyst":
            # Analyst doesn't change node status
            status = (
                NodeStatus(handler_status) if handler_status else NodeStatus.PROVISIONAL
            )
            # Update planning to mark analyst done, then commit
            return CommitAction(node_id="auth_handler", status=status)
        else:
            # Builder - use returned status
            status_str = commit_info.get("status", "provisional")
            try:
                status = NodeStatus(status_str)
            except ValueError:
                status = NodeStatus.PROVISIONAL

            return CommitAction(node_id="auth_handler", status=status)

    # === FLOW CONTROL ===

    # Step 1: Initial builder dispatch
    if not builder_done and handler_status == "near":
        print(f"[PHASE94] Turn {turn}: DISPATCH builder", file=sys.stderr)
        return DispatchAction(
            node_id="auth_handler",
            role="builder",
            weight="lean",
            manager_note="Build auth handler. Use jwt.verify() method (will fail - correct is verify_token)",
        )

    # Step 2: After builder committed, mark builder_done
    if not builder_done and handler_status == "provisional":
        print(f"[PHASE94] Turn {turn}: Mark builder_done", file=sys.stderr)
        return update_plan({"builder_done"})

    # Step 3: Dispatch analyst after builder provisional
    # Check if findings already exist (analyst was already dispatched and committed)
    # Note: analyst_findings is directly in the board node view, not under metadata
    if builder_done and not analyst_done and handler_status == "provisional":
        findings = handler.get("analyst_findings")
        if findings:
            # Findings already committed, mark analyst_done and skip to redispatch
            print(
                f"[PHASE94] Turn {turn}: Mark analyst_done (findings already present)",
                file=sys.stderr,
            )
            return update_plan({"analyst_done"})

        print(f"[PHASE94] Turn {turn}: DISPATCH analyst", file=sys.stderr)
        return DispatchAction(
            node_id="auth_handler",
            role="analyst",
            weight="lean",
            scope="node",
            query="Analyze auth_handler implementation. Identify contract deviation: jwt_util exports verify_token() but auth_handler calls verify().",
        )

    # Step 4: After analyst committed, check for findings and mark analyst_done
    # Note: This is now handled in Step 3 to prevent analyst redispatch loop
    # Keeping this as fallback in case we reach here without findings
    if builder_done and not analyst_done and handler_status == "provisional":
        findings = handler.get("analyst_findings")
        if findings:
            print(
                f"[PHASE94] Turn {turn}: Mark analyst_done (findings present)",
                file=sys.stderr,
            )
            return update_plan({"analyst_done"})
        # No findings yet and no pending work - this shouldn't happen
        # Fall through to let other steps handle

    # Step 5: Redispatch builder after analyst findings committed
    # Only redispatch if we haven't already (no pending commits/returns)
    already_redispatched = (
        "auth_handler" in pending_returns or "auth_handler" in pending_commits
    )
    if (
        analyst_done
        and not redispatch_done
        and handler_status == "provisional"
        and not already_redispatched
    ):
        print(f"[PHASE94] Turn {turn}: REDISPATCH builder", file=sys.stderr)
        return RedispatchAction(
            node_id="auth_handler",
            role="builder",
            weight="standard",
            manager_note="Retry with analyst findings. Use jwt_util.verify_token() not verify().",
        )

    # Step 6: After redispatch, mark redispatch_done
    if not redispatch_done and handler_status == "provisional":
        # Check retry_count to confirm redispatch happened
        retry_count = handler.get("retry_count", 0)
        if retry_count > 0:
            print(
                f"[PHASE94] Turn {turn}: Mark redispatch_done (retry_count={retry_count})",
                file=sys.stderr,
            )
            return update_plan({"redispatch_done"})

    # Step 7: After retry
    if redispatch_done:
        if handler_status == "grounded":
            print(f"[PHASE94] Turn {turn}: SEAL - success", file=sys.stderr)
            return SealAction(summary="Phase 9.4 complete")
        else:
            print(f"[PHASE94] Turn {turn}: HALT after retry", file=sys.stderr)
            return HaltAction(
                reason=f"Phase 9.4 retry complete. Status: {handler_status}. "
                f"Check telemetry for analyst_findings visibility."
            )

    # Fallback
    print(f"[PHASE94] Turn {turn}: HALT (fallback)", file=sys.stderr)
    return HaltAction(
        reason=f"Phase 9.4 fallback. Status: {handler_status}, flags: {flow_flags}"
    )


def verify_retry_context(repo_path: str) -> dict:
    """
    Post-run verification of phase 9.4 requirements.

    Checks:
    1. analyst_findings persisted in metadata
    2. chronicle has dispatch/redispatch entries
    3. retry_count > 0 if redispatch occurred
    """
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from repository import Repository

    repo = Repository(Path(repo_path))
    doc = repo._load()

    handler = doc.get("nodes", {}).get("auth_handler", {})
    metadata = handler.get("metadata", {}) or {}

    results = {
        "passed": False,
        "checks": {},
    }

    # Check 1: analyst_findings present
    findings = metadata.get("analyst_findings")
    results["checks"]["analyst_findings_present"] = bool(findings)
    if findings:
        if isinstance(findings, list) and findings:
            results["checks"]["analyst_findings_scope"] = findings[-1].get("scope")
        elif isinstance(findings, dict):
            results["checks"]["analyst_findings_scope"] = findings.get("scope")

    # Check 2: chronicle dispatch entries
    chronicle = metadata.get("chronicle", [])
    dispatch_count = sum(
        1 for e in chronicle if e.get("action") in ("dispatch", "redispatch")
    )
    results["checks"]["chronicle_dispatch_count"] = dispatch_count
    results["checks"]["has_redispatch"] = any(
        e.get("action") == "redispatch" for e in chronicle
    )

    # Check 3: retry_count
    results["checks"]["retry_count"] = handler.get("retry_count", 0)

    # Check 4: session logs exist
    sessions = repo.get_sessions_for_node("auth_handler")
    results["checks"]["session_count"] = len(sessions)

    # Overall pass: findings present + redispatch happened
    results["passed"] = (
        results["checks"]["analyst_findings_present"]
        and results["checks"]["has_redispatch"]
    )

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Phase 9.4 test verification")
    parser.add_argument("repo_path", help="Path to session repo")
    args = parser.parse_args()

    results = verify_retry_context(args.repo_path)
    print(json.dumps(results, indent=2))
    sys.exit(0 if results["passed"] else 1)
