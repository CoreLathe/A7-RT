"""
Harness validation mixin — schema checking and auto-validation gates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from a7_rt_core.core.models import (
    ManagerState,
    NodeStatus,
    SuspensionReason,
    SuspensionType,
    ValidateAction,
)
from a7_rt_core.harness._common import _now, _update_state
from a7_rt_core.harness.control import HaltSignal

if TYPE_CHECKING:
    from harness.control import ControlMixin


class ValidateMixin:
    """Schema validation and auto-validation gates."""

    def _handle_validate(
        self: "ControlMixin", action: ValidateAction, state: ManagerState
    ) -> ManagerState:
        """
        Schema-check a pending SubagentReturn.
        On pass: move from pending_returns → validated_returns.
        On fail: suspend the node as wild, discard the return.
        """
        node_id = action.node_id
        result = self._pending_returns.pop(node_id, None)

        if result is None:
            return self._log_validate_error(node_id, state, "not in pending_returns")

        valid, error = self._validator.validate_return(result, node_id)
        if not valid:
            return self._suspend_wild(node_id, state, error)

        self._validated_returns[node_id] = result
        self._log_validate_success(node_id, state, result)

        if result.status == NodeStatus.SUSPENDED:
            return self._apply_suspended_result(node_id, state, result)

        return state

    def _suspend_wild(
        self: "ControlMixin", node_id: str, state: ManagerState, error: str
    ) -> ManagerState:
        """
        Suspend node as WILD due to schema validation failure.
        HALT if this is the 2nd wild suspension for this node.
        """
        # Check if node is in terminal state (poisoned) - cannot transition
        doc = self.repo._load()
        current_status = doc.get("nodes", {}).get(node_id, {}).get("status")
        if current_status == NodeStatus.POISONED.value:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "wild_suspension_ignored",
                    "target": node_id,
                    "detail": f"Schema error: {error}; node is poisoned (terminal)",
                }
            )
            new_in_flight = [n for n in state.in_flight if n != node_id]
            return _update_state(state, in_flight=new_in_flight)

        # Check if node is already suspended - idempotent, just clear from in_flight
        if current_status == NodeStatus.SUSPENDED.value:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "wild_suspension_idempotent",
                    "target": node_id,
                    "detail": f"Schema error: {error}; node already suspended",
                }
            )
            new_in_flight = [n for n in state.in_flight if n != node_id]
            return _update_state(state, in_flight=new_in_flight)

        reason = SuspensionReason(type=SuspensionType.WILD, detail=f"Schema error: {error}")
        self.repo.update_node(
            node_id,
            status=NodeStatus.SUSPENDED,
            suspension_reason=reason.model_dump(),
        )

        self._wild_counts[node_id] = self._wild_counts.get(node_id, 0) + 1
        wild_count = self._wild_counts[node_id]

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "wild_suspension",
                "target": node_id,
                "detail": f"Schema error: {error}",
                "wild_count": wild_count,
                "wild_source": "schema",
            }
        )

        new_in_flight = [n for n in state.in_flight if n != node_id]

        if wild_count >= 2:
            state = _update_state(state, in_flight=new_in_flight)
            self._checkpoint(state)
            raise HaltSignal(
                f"Node '{node_id}' has been wild-suspended {wild_count} times. "
                "Invariant: same node wild twice → HALT. Human must intervene."
            )

        return _update_state(state, in_flight=new_in_flight)

    def _apply_suspended_result(
        self: "ControlMixin",
        node_id: str,
        state: ManagerState,
        result,
    ) -> ManagerState:
        """Auto-apply SUSPENDED result — no COMMIT needed."""
        # Check if node is already in a terminal state (poisoned) - can't transition
        doc = self.repo._load()
        current_status = doc.get("nodes", {}).get(node_id, {}).get("status")
        if current_status == NodeStatus.POISONED.value:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "suspended_result_ignored",
                    "target": node_id,
                    "detail": "Node is poisoned (terminal); SUSPENDED result ignored",
                }
            )
            new_in_flight = [n for n in state.in_flight if n != node_id]
            return _update_state(state, in_flight=new_in_flight)

        # Check if node is already suspended - idempotent, just clear in_flight
        if current_status == NodeStatus.SUSPENDED.value:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "suspended_result_idempotent",
                    "target": node_id,
                    "detail": "Node already suspended; clearing from in_flight",
                }
            )
            new_in_flight = [n for n in state.in_flight if n != node_id]
            return _update_state(state, in_flight=new_in_flight)

        if result.suspension_reason is None:
            result.suspension_reason = SuspensionReason(
                type=SuspensionType.NEAR,
                detail="Subagent returned suspended without reason",
            )

        self.repo.update_node(
            node_id,
            status=NodeStatus.SUSPENDED,
            suspension_reason=result.suspension_reason.model_dump(),
        )

        new_in_flight = [n for n in state.in_flight if n != node_id]
        return _update_state(state, in_flight=new_in_flight)

    def _log_validate_error(
        self: "ControlMixin", node_id: str, state: ManagerState, detail: str
    ) -> ManagerState:
        """Log validation error and return unchanged state."""
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "validate_error",
                "target": node_id,
                "detail": f"VALIDATE called but '{node_id}' {detail}",
            }
        )
        return state

    def _log_validate_success(
        self: "ControlMixin", node_id: str, state: ManagerState, result
    ) -> None:
        """Log successful validation to chronicle and events."""
        self._log_chronicle_entry(
            node_id=node_id,
            actor="manager",
            action="validate",
            metadata={
                "auto": False,
                "status": result.status.value,
            },
            state=state,
        )

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "validated",
                "target": node_id,
                "detail": f"Schema valid; status={result.status.value}",
            }
        )
