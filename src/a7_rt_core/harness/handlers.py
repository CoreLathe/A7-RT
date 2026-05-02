"""
HandlersMixin — CONSULT, SUSPEND, SEAL, and UPDATE_PLAN action handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from a7_rt_core.core.models import (
    ConsultAction,
    InterfaceContract,
    ManagerMode,
    ManagerState,
    NodeStatus,
    SealAction,
    SuspendAction,
    SuspensionReason,
    UpdatePlanAction,
)
from a7_rt_core.harness._common import _now, _update_state

if TYPE_CHECKING:
    from a7_rt_core.harness.control import ControlMixin


class HandlersMixin:
    """
    Miscellaneous action handlers that don't fit into the main CRUD flow.
    """

    def _handle_consult(
        self: "ControlMixin", action: ConsultAction, state: ManagerState
    ) -> ManagerState:
        """
        Call the A7 consulting engine (if wired); store result in pending_consult.
        If no consult_hook is configured, logs and continues.
        """
        from a7_rt_core.context.core import analyst_view

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "manager",
                "action": "consult",
                "target": str(action.relevant_node_ids),
                "detail": action.question,
            }
        )

        if self._consult_hook is not None:
            try:
                doc = self.repo._load()
                ctx = analyst_view(doc, action.question, action.relevant_node_ids)
                verdict = self._consult_hook(
                    action.question,
                    ctx,
                    action.constraints,
                )
                self._pending_consult = (
                    verdict if isinstance(verdict, dict) else {"verdict": str(verdict)}
                )
            except Exception as e:
                self._pending_consult = {
                    "verdict": f"CONSULT error: {e}",
                    "confidence": "void",
                }
        else:
            self._pending_consult = {
                "verdict": "No consult hook configured — human must decide.",
                "confidence": "void",
            }

        # Flag that we issued a CONSULT this turn (prevents immediate clear)
        self._consult_issued_this_turn = True

        return state

    def _handle_suspend(
        self: "ControlMixin", action: SuspendAction, state: ManagerState
    ) -> ManagerState:
        """Mark a node blocked. Creates an immediate suspension without dispatch."""
        from a7_rt_core.storage.repository import InvariantViolation, RepositoryError

        node_id = action.node_id

        # Check if node is already suspended - idempotent
        doc = self.repo._load()
        current_status = doc.get("nodes", {}).get(node_id, {}).get("status")
        if current_status == NodeStatus.SUSPENDED.value:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "suspend_idempotent",
                    "target": node_id,
                    "detail": "Node already suspended; skipping redundant SUSPEND",
                }
            )
            return state

        reason = SuspensionReason(type=action.type, detail=action.detail)
        try:
            self.repo.update_node(
                node_id,
                status=NodeStatus.SUSPENDED,
                suspension_reason=reason.model_dump(),
            )
        except (ValueError, InvariantViolation) as e:
            from a7_rt_core.harness.control import HaltSignal

            raise HaltSignal(f"SUSPEND failed for '{node_id}': {e}") from e

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "manager",
                "action": "suspend",
                "target": node_id,
                "detail": f"type={action.type.value}: {action.detail}",
            }
        )

        # Log SUSPEND chronicle entry
        self._log_chronicle_entry(
            node_id=node_id,
            actor="manager",
            action="suspend",
            metadata={
                "type": action.type.value,
                "detail": action.detail,
            },
            state=state,
        )

        # Remove from in_flight if it was pending
        new_in_flight = [n for n in state.in_flight if n != node_id]
        self._pending_returns.pop(node_id, None)
        self._validated_returns.pop(node_id, None)
        return _update_state(state, in_flight=new_in_flight)

    def _handle_seal(self: "ControlMixin", action: SealAction, state: ManagerState) -> ManagerState:
        """
        Attempt to seal the current stage. Harness validates all nodes resolved.
        On success: manager lifecycle ends after this turn.
        On failure (InvariantViolation): logs error to board for next turn.
        """
        from a7_rt_core.storage.repository import InvariantViolation, RepositoryError

        stage_id = state.current_stage_id
        doc = self.repo._load()
        stage = doc.get("stages", {}).get(stage_id, {})
        node_ids = stage.get("node_ids", [])

        # Extract planning narrative for stage summary if present
        planning_narrative = state.planning.narrative if state.planning else None
        summary = action.summary
        if planning_narrative:
            summary = f"{summary}\n\nPlanning narrative: {planning_narrative}".strip()

        # Build enriched exported_interfaces with mechanical metadata
        exported_interfaces: dict[str, dict] = {}
        from a7_rt_core.harness.utils import _extract_signatures

        for nid in node_ids:
            node = doc["nodes"].get(nid, {})
            if node.get("status") == NodeStatus.GROUNDED.value:
                iface_data = node.get("interface", {})
                metadata = node.get("metadata") or {}
                export_names = iface_data.get("exports", [])

                # Extract signatures for exports
                export_signatures: list[str] = []
                content_file = node.get("content_file")
                if content_file and export_names:
                    content_path = self.repo._content_dir / content_file
                    if content_path.exists():
                        try:
                            content = content_path.read_text()
                            export_signatures = _extract_signatures(
                                content, content_file, export_names
                            )
                        except Exception:
                            pass  # Silent fail on extraction error

                exported_interfaces[nid] = {
                    "exports": export_names,
                    "assumptions": iface_data.get("assumptions", []),
                    "export_signatures": export_signatures,
                    "lines": metadata.get("lines", 0),
                    "tokens": metadata.get("tokens", 0),
                    "plumbing_summary": metadata.get("plumbing_summary", ""),
                }

        # Auto-clear analyst_findings for all nodes in this stage before sealing
        for nid in node_ids:
            node = doc["nodes"].get(nid, {})
            metadata = node.get("metadata") or {}
            if metadata.get("analyst_findings"):
                metadata_update = dict(metadata)
                metadata_update["analyst_findings"] = []
                self.repo.update_node(nid, metadata=metadata_update)

        # Convert exported_interfaces to InterfaceContract objects for seal_stage
        exported_contracts: dict[str, InterfaceContract] = {}
        for nid, data in exported_interfaces.items():
            exported_contracts[nid] = InterfaceContract(
                exports=data["exports"],
                assumptions=data["assumptions"],
            )

        try:
            self.repo.seal_stage(stage_id, summary, exported_contracts)
        except (InvariantViolation, RepositoryError) as e:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "seal_rejected",
                    "target": stage_id,
                    "detail": str(e),
                }
            )
            # Surface the rejection on the next board via last_error so the
            # manager can read it and handle blockers before retrying SEAL.
            self._last_error = f"SEAL rejected: {e}"
            return state  # manager will see the error on next board
        except Exception as e:
            from a7_rt_core.harness.control import HaltSignal

            raise HaltSignal(f"SEAL failed unexpectedly: {e}") from e

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "manager",
                "action": "seal",
                "target": stage_id,
                "detail": action.summary,
            }
        )
        # Kill the lifecycle — stage sealed, manager's job is done
        self.kill(state, f"stage {stage_id} sealed")
        return _update_state(state, mode=ManagerMode.DEAD)

    def _handle_update_plan(
        self: "ControlMixin", action: UpdatePlanAction, state: ManagerState
    ) -> ManagerState:
        """
        Update manager planning state: narrative intent and pre-SEAL checklist.
        Full replacement semantics — manager emits complete new state.
        """
        from a7_rt_core.core.models import PlanningState

        new_planning = PlanningState(
            narrative=action.narrative,
            checklist=action.checklist or [],
            updated_at=state.turn,
        )
        new_state = _update_state(state, planning=new_planning)
        self._checkpoint(new_state)
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "manager",
                "action": "update_plan",
                "detail": f"narrative={action.narrative[:50] if action.narrative else None}, items={len(action.checklist)}",
            }
        )
        return new_state
