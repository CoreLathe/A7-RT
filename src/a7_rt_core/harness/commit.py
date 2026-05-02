"""
CommitMixin — Git commit and finalization logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from a7_rt_core.core.models import CommitAction, ManagerState, NodeStatus
from a7_rt_core.harness._common import _now, _update_state

if TYPE_CHECKING:
    from harness.control import ControlMixin


class CommitMixin:
    """
    Handles COMMIT action: apply validated returns, finalize to disk, git commit.
    """

    def _handle_commit(
        self: "ControlMixin", action: CommitAction, state: ManagerState
    ) -> ManagerState:
        """
        Apply a validated SubagentReturn to master.json.

        Manager declares the final status via action.status. The harness
        applies the return, propagates poison if needed, and git-commits.

        Requires the node to be in validated_returns — VALIDATE must precede COMMIT.
        """
        node_id = action.node_id

        result = self._validated_returns.pop(node_id, None)
        if result is None:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "commit_error",
                    "target": node_id,
                    "detail": f"COMMIT called but '{node_id}' not in validated_returns (must VALIDATE first)",
                }
            )
            return state

        # Manager's declared status overrides result.status if they differ.
        effective_status = action.status

        # Check if node is already at target status (e.g., on resume after crash)
        doc = self.repo._load()
        current_status = doc.get("nodes", {}).get(node_id, {}).get("status")
        if current_status == effective_status.value:
            # For analyst returns, still apply findings even if status unchanged
            if result.role == "analyst" and result.analysis_result:
                state = self._apply_return(node_id, result, effective_status, state)
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "commit_skipped",
                    "target": node_id,
                    "detail": f"Node already at status '{current_status}', skipping redundant commit",
                }
            )
            new_in_flight = [n for n in state.in_flight if n != node_id]
            return _update_state(state, in_flight=new_in_flight)

        state = self._apply_return(node_id, result, effective_status, state)

        # For test_author: explicitly transition NEAR -> PROVISIONAL (contract defined)
        # test_author writes test contract; status should advance to reflect this
        if result.role == "test_author" and current_status == NodeStatus.NEAR.value:
            self.repo.update_node(node_id, status=NodeStatus.PROVISIONAL)
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "status_advanced",
                    "target": node_id,
                    "detail": "test_author contract defined: near -> provisional",
                }
            )

        # Log COMMIT chronicle entry
        self._log_chronicle_entry(
            node_id=node_id,
            actor="manager",
            action="commit",
            metadata={
                "status": effective_status.value,
                "reason": action.reason,
                "clear_findings": getattr(action, "clear_findings", None),
            },
            state=state,
        )

        # Handle clear_findings if specified
        clear_findings = getattr(action, "clear_findings", None)
        if clear_findings:
            self._handle_clear_findings(node_id, clear_findings, state)

        # If the manager annotated the commit, append note to retry_context.
        if action.manager_note:
            doc = self.repo._load()
            node_data = doc["nodes"].get(node_id, {})
            current_ctx = list(node_data.get("retry_context", []))
            current_ctx.append(
                {
                    "turn": state.turn,
                    "manager_note": action.manager_note,
                }
            )
            self.repo.update_node(node_id, retry_context=current_ctx)

        new_in_flight = [n for n in state.in_flight if n != node_id]
        return _update_state(state, in_flight=new_in_flight)

    def _git_commit(self: "ControlMixin", message: str) -> None:
        """
        Commit all tracked changes in git_root with *message*.
        Silently no-ops if gitpython is not installed or nothing to commit.
        """
        if self.git_root is None:
            return
        try:
            import git  # type: ignore

            repo = git.Repo(self.git_root)
            repo.git.add(A=True)
            if repo.is_dirty(index=True, working_tree=False):
                repo.index.commit(message)
        except Exception:
            # Git errors are non-fatal — the session state is already on disk
            pass
