"""
PersistenceMixin — Checkpointing, resume, and event draining.
"""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING, Any

from a7_rt_core.core.models import (
    ManagerState,
    NodeStatus,
    PlanningState,
    SuspensionReason,
    SuspensionType,
)
from a7_rt_core.harness._common import _now, _update_state

if TYPE_CHECKING:
    from a7_rt_core.harness.control import ControlMixin


class PersistenceMixin:
    """
    Handles persistence: checkpointing state, serializing pending commits,
    rebuilding state on resume, and draining human input events.
    """

    def _checkpoint(self: "ControlMixin", state: ManagerState) -> None:
        """Write manager.json and optionally commit."""
        if state.is_dead:
            return  # dead managers use manager_seal, not checkpoint
        # Sync wild_counts and pending_commits to state for persistence across resume
        pending_commits_data = self._serialize_pending_commits()
        state = _update_state(
            state, wild_counts=self._wild_counts, pending_commits=pending_commits_data
        )
        self.repo.manager_checkpoint(state)
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "checkpoint",
                "target": state.current_stage_id,
                "detail": f"mode={state.mode.value}",
            }
        )

    def _serialize_pending_commits(self: "ControlMixin") -> dict[str, Any]:
        """
        Serialize _validated_returns for persistence in ManagerState.
        Stores minimal data needed to reconstruct SubagentReturn on resume.
        """
        result: dict[str, Any] = {}
        for node_id, ret in self._validated_returns.items():
            result[node_id] = {
                "node_id": node_id,
                "role": getattr(ret, "role", "builder"),
                "status": getattr(ret, "status", NodeStatus.PROVISIONAL).value,
                "files": list(getattr(ret, "files", {}).keys()),
                "pr_note": getattr(ret, "pr_note", None),
                "suspension_reason": (
                    ret.suspension_reason.model_dump()
                    if getattr(ret, "suspension_reason", None)
                    else None
                ),
                "interface_update": getattr(ret, "interface_update", None),
                "new_deps": getattr(ret, "new_deps", []),
                "file_tags": [
                    tag.model_dump() if hasattr(tag, "model_dump") else tag
                    for tag in getattr(ret, "file_tags", [])
                ],
                "edits": getattr(ret, "edits", []),
            }
        return result

    def _rebuild_validated_returns(self: "ControlMixin", pending_commits: dict[str, Any]) -> dict:
        """
        Rebuild _validated_returns from persisted pending_commits on resume.
        Creates SubagentReturn objects with the data that was saved at checkpoint.
        """
        from a7_rt_core.core.models import (
            FileTag,
            NodeStatus,
            SuspensionReason,
            SuspensionType,
        )

        result: dict = {}
        for node_id, data in pending_commits.items():
            # Deserialize suspension_reason if present
            suspension_reason = None
            sr_data = data.get("suspension_reason")
            if sr_data:
                suspension_reason = SuspensionReason(
                    type=SuspensionType(sr_data.get("type", "near")),
                    detail=sr_data.get("detail", ""),
                )

            # Deserialize file_tags
            file_tags = []
            for tag_data in data.get("file_tags", []):
                file_tags.append(
                    FileTag(
                        node_id=tag_data.get("node_id", ""),
                        author_role=tag_data.get("author_role", "builder"),
                        author_turn=tag_data.get("author_turn", 0),
                        category=tag_data.get("category", "quirk"),
                        content=tag_data.get("content", ""),
                        propagate=tag_data.get("propagate", True),
                        confirmed_by=tag_data.get("confirmed_by", []),
                    )
                )

            # Reconstruct SubagentReturn
            # Note: We don't have the full file contents - they should already be
            # written to disk by _apply_return. On resume, we just need enough
            # metadata for the manager to issue COMMIT.
            from a7_rt_core.core.models import SubagentReturn

            ret = SubagentReturn(
                status=NodeStatus(data.get("status", "provisional")),
                role=data.get("role", "builder"),
                files={},  # Content already on disk
                pr_note=data.get("pr_note"),
                suspension_reason=suspension_reason,
                interface_update=data.get("interface_update"),
                new_deps=data.get("new_deps", []),
                file_tags=file_tags,
                edits=data.get("edits", []),
            )
            result[node_id] = ret

        return result

    def _drain_events(self: "ControlMixin", state: ManagerState) -> ManagerState:
        """
        Process all pending human-input events.
        Routes /halt, /suspend, /verify slash commands. Other input is logged as-is.
        Unrecognized slash commands are logged with a warning so the manager sees them.
        """
        while not self._human_queue.empty():
            try:
                event = self._human_queue.get_nowait()
            except queue.Empty:
                break

            detail = event.get("detail", str(event))

            if detail.startswith("/halt"):
                reason = detail[len("/halt") :].strip() or "human halt"
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "human",
                        "action": "input",
                        "target": state.current_stage_id,
                        "detail": detail,
                    }
                )
                self._checkpoint(state)
                from harness.control import HaltSignal

                raise HaltSignal(reason)

            elif detail.startswith("/suspend"):
                parts = detail[len("/suspend") :].strip().split(None, 1)
                if len(parts) >= 1:
                    node_id = parts[0]
                    suspend_detail = parts[1] if len(parts) > 1 else "human suspend"
                    try:
                        self.repo.update_node(
                            node_id,
                            status=NodeStatus.SUSPENDED,
                            suspension_reason=SuspensionReason(
                                type=SuspensionType.FAR,
                                detail=suspend_detail,
                            ),
                        )
                        self.repo.append_event(
                            {
                                "turn": state.turn,
                                "timestamp": _now(),
                                "actor": "human",
                                "action": "human_suspend",
                                "target": node_id,
                                "detail": suspend_detail,
                            }
                        )
                    except Exception:
                        pass  # node not found — skip silently

            elif detail.startswith("/verify"):
                parts = detail[len("/verify") :].strip().split(None, 1)
                if len(parts) == 2:
                    from_node, to_node = parts
                    try:
                        self.repo.verify_assumption(from_node, to_node)
                        self.repo.append_event(
                            {
                                "turn": state.turn,
                                "timestamp": _now(),
                                "actor": "human",
                                "action": "human_verify",
                                "target": from_node,
                                "detail": f"assumption edge {from_node} → {to_node} verified",
                            }
                        )
                    except Exception:
                        pass  # edge not found — skip silently

            elif detail.startswith("/intent"):
                text = detail[len("/intent") :].strip()
                # Build new PlanningState preserving existing checklist
                existing = state.planning
                new_planning = PlanningState(
                    narrative=text or None,
                    checklist=existing.checklist if existing else [],
                    updated_at=state.turn,
                )
                state = _update_state(state, planning=new_planning)
                self._checkpoint(state)
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "human",
                        "action": "update_plan",
                        "target": state.current_stage_id,
                        "detail": f"intent set: {text[:50] if text else None}",
                    }
                )

            elif detail.startswith("/"):
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "human",
                        "action": "input",
                        "target": state.current_stage_id,
                        "detail": f"[unrecognized command] {detail}",
                    }
                )

            else:
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "human",
                        "action": "input",
                        "target": state.current_stage_id,
                        "detail": detail,
                    }
                )
                state = _update_state(
                    state,
                    human_input_queue=list(state.human_input_queue)
                    + [{"text": detail, "turn": state.turn}],
                )

        return state
