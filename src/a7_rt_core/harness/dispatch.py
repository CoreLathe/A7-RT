"""DispatchMixin - Subagent spawning, ShadowFS setup, auto-validation gates."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from a7_rt_core.context.core import (
    PreviousAttemptSummary,
    TestOutputCompressor,
    analyst_view,
    assemble_test_author_view,
    builder_view,
    materialize_view,
)
from a7_rt_core.core.models import (
    DispatchAction,
    ManagerMode,
    ManagerState,
    NodeStatus,
    NodeType,
    RedispatchAction,
    SubagentError,
    SuspensionReason,
    SuspensionType,
)
from a7_rt_core.harness._common import _now, _update_state
from a7_rt_core.harness.control import HaltSignal
from a7_rt_core.storage.shadowfs import ShadowFS


class FastPathResult:
    """Result of a fast-path ground attempt."""

    def __init__(self, success: bool, state: "ManagerState", detail: str = ""):
        self.success = success
        self.state = state
        self.detail = detail


class DispatchMixin:
    """Subagent dispatch logic with ShadowFS integration."""

    def _handle_dispatch(self, action: DispatchAction, state: ManagerState) -> ManagerState:
        """
        Call the subagent hook for action.node_id.
        Store the result in pending_returns (awaits VALIDATE next turn).

        - Rejects dispatch in drain mode.
        - On SubagentError: marks node suspended:wild and continues.
        """
        node_id = action.node_id
        role = action.role
        weight = action.weight

        # ENFORCE: test_author must be dispatched before builder for NEAR nodes needing contract
        doc = self.repo._load()
        nodes = doc.get("nodes") or {}
        node_data = nodes.get(node_id) or {}
        node_status = node_data.get("status")
        node_type = node_data.get("type")
        has_exports = bool(node_data.get("interface", {}).get("exports"))
        needs_contract_first = node_status == NodeStatus.NEAR.value and (
            node_type == NodeType.FEATURE.value
            or (node_type == NodeType.GLUE.value and not has_exports)
        )
        if role == "builder" and needs_contract_first:
            # Hard rejection with last_error for manager visibility
            error_msg = (
                f"DISPATCH REJECTED: Node '{node_id}' is {node_status} {node_type} with "
                f"{'no exports' if not has_exports else 'exports defined'}. "
                f"test_author MUST be dispatched before builder to define test_contract. "
                f"Dispatch test_author first, then builder after node reaches 'provisional' status."
            )
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "dispatch_rejected",
                    "target": node_id,
                    "detail": error_msg,
                }
            )
            # Add to chronicle for manager visibility
            # node_data may be falsy if the node doesn't exist; use repo to update safely
            try:
                node_chronicle_data = {
                    "metadata": {
                        "chronicle": [
                            {
                                "turn": state.turn,
                                "actor": "harness",
                                "action": "dispatch_rejected",
                                "metadata": {
                                    "attempted_role": role,
                                    "reason": "test_author_required_first",
                                },
                                "timestamp": _now(),
                            }
                        ]
                    }
                }
                self.repo.update_node(node_id, **node_chronicle_data)
            except Exception:
                # If node doesn't exist or update fails, skip chronicle update
                pass
            self._last_error = error_msg
            return state

        # Fast-path check: provisional node with existing implementation and tests
        if role == "builder" and self._can_fast_path_ground(node_id):
            fast_path_result = self._try_fast_path_ground(node_id, state)
            if fast_path_result.success:
                return fast_path_result.state

        if state.is_dead:
            raise HaltSignal("DISPATCH called with dead manager — invariant violation")
        if state.is_drain:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "dispatch_rejected",
                    "target": node_id,
                    "detail": "DISPATCH rejected: manager is in drain mode",
                }
            )
            return state

        doc = self.repo._load()
        if node_id not in doc["nodes"]:
            raise HaltSignal(f"DISPATCH target '{node_id}' not in master.json")

        # Check for existing active session (one active session per node invariant)
        if self.repo.get_active_session_for_node(node_id):
            raise HaltSignal(
                f"Node {node_id} has an active session. "
                "Cannot dispatch until current session completes."
            )

        # Increment dispatch sequence for this dispatch
        self._current_dispatch_seq += 1

        # Generate session ID and compute attempt number
        session_id = str(uuid.uuid4())
        attempt_number = self.repo.count_sessions_for_node(node_id) + 1

        # Write session_start to log
        self.repo.append_session_log(
            node_id,
            {
                "type": "session_start",
                "session_id": session_id,
                "node_id": node_id,
                "role": role,
                "dispatch_turn": state.turn,
                "attempt_number": attempt_number,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Track active session
        self._active_sessions[node_id] = session_id

        # Create ShadowFS for this dispatch (Phase 3: ShadowFS Integration)
        shadow = ShadowFS(self.repo._content_dir)
        self._current_shadow = shadow
        self._current_node_id = node_id

        # Log shadow creation event
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "shadow_created",
                "target": node_id,
                "detail": f"ShadowFS initialized for {node_id}",
            }
        )

        # Get intent from action if provided
        intent = getattr(action, "intent", None)

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "manager",
                "action": "dispatch",
                "target": node_id,
                "session_id": session_id,
                "intent": intent,
                "detail": f"Dispatching {role} [{weight}] (attempt {attempt_number})",
            }
        )

        # Log DISPATCH chronicle entry
        self._log_chronicle_entry(
            node_id=node_id,
            actor="manager",
            action="dispatch",
            metadata={
                "role": role,
                "weight": weight,
                "session_id": session_id,
                "attempt_number": attempt_number,
                "focus_hints": getattr(action, "focus_hints", None),
                "clear_findings": getattr(action, "clear_findings", None),
            },
            state=state,
            dispatch_seq=self._current_dispatch_seq,
        )

        # Handle clear_findings if specified
        clear_findings = getattr(action, "clear_findings", None)
        if clear_findings:
            self._handle_clear_findings(node_id, clear_findings, state)

        # Assemble context view (no file I/O here — subagent.py reads content_file)

        # Extract Phase 3 dispatch guidance from action
        focus_hints = getattr(action, "focus_hints", None)
        manager_note = getattr(action, "manager_note", None)

        # Get exploration hints for warm redispatch
        exploration_hints = self.repo.get_exploration_hints(node_id)

        if role == "builder":
            # Check for previous attempt context (Phase 2: Fresh-Context Redispatch)
            previous_attempt = getattr(self, "_pending_redispatch_context", {}).pop(node_id, None)

            ctx = builder_view(
                doc,
                node_id,
                focus_hints=focus_hints,
                manager_note=manager_note,
                exploration_hints=exploration_hints,
                previous_attempt=previous_attempt,
            )
        elif role == "test_author":
            ctx = assemble_test_author_view(doc, node_id)
        elif role == "analyst":
            # Analyst three-scope dispatch: node | project | external
            scope = getattr(action, "scope", "node")
            query = getattr(action, "query", f"Analyze {node_id}")
            target_nodes = getattr(action, "target_nodes", [node_id])

            if scope == "node":
                # Single node examination
                target_nodes = target_nodes or [node_id]
                ctx = analyst_view(doc, query, target_nodes, protocol_weight=weight)
            elif scope == "project":
                # Multi-node cross-reference
                target_nodes = target_nodes or [node_id]
                ctx = analyst_view(doc, query, target_nodes, protocol_weight=weight)
            elif scope == "external":
                # External research - may have empty target_nodes
                ctx = analyst_view(doc, query, target_nodes or [], protocol_weight=weight)
            else:
                # Fallback to node scope
                ctx = analyst_view(doc, query, [node_id], protocol_weight=weight)
        else:
            ctx = builder_view(
                doc,
                node_id,
                focus_hints=focus_hints,
                manager_note=manager_note,
                exploration_hints=exploration_hints,
            )  # fallback

        # Materialize file content into the view (line numbers, etc.)
        ctx_materialized = materialize_view(ctx, base_path=self.repo._content_dir)

        # Create tool call callback for session logging
        def on_tool_call(tool_name: str, args: dict, turn: int, intent: str | None = None) -> None:
            """Called after each tool execution to write to session log."""
            self.repo.append_session_log(
                node_id,
                {
                    "type": "tool_call",
                    "session_id": session_id,
                    "turn": turn,
                    "tool": tool_name,
                    "args": args,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "intent": intent,
                },
            )
            # Note: tool calls are NOT logged to chronicle (agent scratchpad boundary)

        # Apply rate limiting delay before subagent call
        self._apply_inter_call_delay()

        try:
            # Pass shadow to subagent hook for tool attachment
            result = self._subagent_hook(
                node_id,
                role,
                ctx_materialized,
                session_id=session_id,
                on_tool_call=on_tool_call,
                shadow=shadow,
            )
        except SubagentError as e:
            # Write session_end for failed session
            self.repo.append_session_log(
                node_id,
                {
                    "type": "session_end",
                    "session_id": session_id,
                    "status": "failed",
                    "iterations_used": 0,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            del self._active_sessions[node_id]

            reason = SuspensionReason(type=SuspensionType.WILD, detail=str(e))
            self.repo.update_node(
                node_id,
                status=NodeStatus.SUSPENDED,
                suspension_reason=reason.model_dump(),
            )
            self._wild_counts[node_id] = self._wild_counts.get(node_id, 0) + 1
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "wild_suspension",
                    "target": node_id,
                    "detail": str(e),
                    "wild_count": self._wild_counts[node_id],
                    "wild_source": "subagent_error",
                }
            )
            if self._wild_counts[node_id] >= 2:
                self._checkpoint(state)
                raise HaltSignal(
                    f"Node '{node_id}' has been wild-suspended "
                    f"{self._wild_counts[node_id]} times this session. "
                    "Invariant: same node wild twice → HALT. Human must intervene."
                )
            return state

        # Determine session status based on result
        session_status = "completed"
        if result.status == NodeStatus.SUSPENDED:
            session_status = "suspended"
        elif result.status == NodeStatus.POISONED:
            session_status = "failed"

        # Write session_end with files_written for exploration_hints
        self.repo.append_session_log(
            node_id,
            {
                "type": "session_end",
                "session_id": session_id,
                "status": session_status,
                "iterations_used": getattr(result, "iterations_used", 0),
                "files_written": list(result.files.keys()) if result.files else [],
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        del self._active_sessions[node_id]

        # Add session_id to result for tracking
        result.session_id = session_id

        # Log agent RETURN chronicle entry
        self._log_chronicle_entry(
            node_id=node_id,
            actor="agent",
            action="return",
            metadata={
                "status": result.status.value,
                "exports": list(result.interface_update.get("exports", [])),
                "files_written": list(result.files.keys()) if result.files else [],
                "pr_note": result.pr_note,
                "iterations_used": getattr(result, "iterations_used", 0),
                "tool_usage": getattr(result, "tool_usage", {}),
            },
            state=state,
            dispatch_seq=self._current_dispatch_seq,
        )

        # Analyst findings persistence: Always apply immediately (metadata only, no files)
        # This happens before validation/escalation checks to ensure findings are captured
        if result.role == "analyst" and result.analysis_result:
            # Get current node status to preserve it (analyst doesn't change status)
            doc = self.repo._load()
            current_status = doc.get("nodes", {}).get(node_id, {}).get("status", "near")
            state = self._apply_return(node_id, result, NodeStatus(current_status), state)

        # Phase 2: Fresh-Context Redispatch - Package attempt summary if checkpoint reached
        if getattr(result, "checkpoint_reached", False):
            # Package this attempt for potential redispatch
            previous_attempt = PreviousAttemptSummary(
                dispatch_number=attempt_number,
                files_written=list(result.files.keys()) if result.files else [],
                files_modified=self._detect_modified_files(node_id, result.files),
                test_result=TestOutputCompressor.compress(
                    self._get_test_output(node_id), max_failures=3
                )
                if self._has_test_output(node_id)
                else None,
                thoughts=getattr(result, "thoughts_recorded", []),
                final_status="checkpoint",
                error_classification=self._classify_dispatch_result(result),
            )

            # Store for next dispatch
            if not hasattr(self, "_pending_redispatch_context"):
                self._pending_redispatch_context: dict[str, PreviousAttemptSummary] = {}
            self._pending_redispatch_context[node_id] = previous_attempt

            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "checkpoint_reached",
                    "target": node_id,
                    "detail": f"Checkpoint at iteration {getattr(result, 'iterations_used', 0)}",
                    "iteration_status": getattr(result, "iteration_status", None),
                }
            )

        # Phase 4: Auto-VALIDATE check
        if self._can_auto_validate(result, node_id):
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "auto_validate",
                    "target": node_id,
                    "detail": f"Auto-validated {result.status.value}",
                }
            )
            # Skip manager VALIDATE, move directly to validated state
            self._validated_returns[node_id] = result

            # Auto-commit check for grounded
            if self._can_auto_commit(result, node_id):
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "auto_commit",
                        "target": node_id,
                        "detail": f"Auto-committed with {len(result.test_runs)} test runs",
                    }
                )
                # Apply return immediately, skip manager COMMIT
                state = self._apply_return(node_id, result, result.status, state)
                new_in_flight = [n for n in state.in_flight if n != node_id]
                return _update_state(state, in_flight=new_in_flight)

            new_in_flight = list(state.in_flight) + [node_id]
            return _update_state(state, in_flight=new_in_flight)

        # Standard flow: store in pending_returns for manager VALIDATE
        self._pending_returns[node_id] = result
        new_in_flight = list(state.in_flight) + [node_id]

        # Escalation signal: log so the manager sees it on the next board
        if getattr(result, "escalate", False):
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "escalate_requested",
                    "target": node_id,
                    "detail": (
                        f"Subagent [{role}] requested escalation for {node_id}. "
                        f"Current weight: {weight}. Re-dispatch at higher weight after COMMIT."
                    ),
                }
            )

        return _update_state(state, in_flight=new_in_flight)

    def _handle_redispatch(self, action: RedispatchAction, state: ManagerState) -> ManagerState:
        """
        Re-dispatch a previously failed node with retry context injected.

        1. Load node; check retry_count against max_retries - HALT if exhausted.
        2. Append manager_note to retry_context and increment retry_count on disk.
        3. Call subagent hook (same as DISPATCH, but with enriched context view).
        4. Log 'redispatch' event (distinct from 'dispatch' in the audit log).
        5. Park result in pending_returns - manager must VALIDATE next.

        Drain-mode behaviour mirrors DISPATCH: REDISPATCH is also rejected in drain.
        """
        node_id = action.node_id
        role = action.role
        weight = action.weight

        if state.is_dead:
            raise HaltSignal("REDISPATCH called with dead manager — invariant violation")
        if state.is_drain:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "redispatch_rejected",
                    "target": node_id,
                    "detail": "REDISPATCH rejected: manager is in drain mode",
                }
            )
            return state

        doc = self.repo._load()
        if node_id not in doc["nodes"]:
            raise HaltSignal(f"REDISPATCH target '{node_id}' not in master.json")

        node_data = doc["nodes"][node_id]
        retry_count = node_data.get("retry_count", 0)
        max_retries = node_data.get("max_retries", 3)

        if retry_count >= max_retries:
            self._checkpoint(state)
            raise HaltSignal(
                f"Node '{node_id}' has reached max_retries={max_retries}. "
                "Human must intervene before further redispatch."
            )

        # Unpoison poisoned roots (poisoned_by: null) to enable retry
        # Philosophy: Downstream of ☠ is ☠, but roots can be fixed
        current_status = node_data.get("status")
        poisoned_by = node_data.get("poisoned_by")
        if current_status == NodeStatus.POISONED.value and poisoned_by is None:
            self.repo.update_node(
                node_id,
                status=NodeStatus.NEAR,
                poisoned_by=None,  # Clear any stale data
            )
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "unpoison",
                    "target": node_id,
                    "detail": "Poisoned root reverted to near for redispatch",
                }
            )

            # Propagate unpoison to downstream nodes poisoned by this root
            for other_id, other_node in doc.get("nodes", {}).items():
                if (
                    other_node.get("status") == NodeStatus.POISONED.value
                    and other_node.get("poisoned_by") == node_id
                ):
                    self.repo.update_node(
                        other_id,
                        status=NodeStatus.NEAR,
                        poisoned_by=None,
                    )
                    self.repo.append_event(
                        {
                            "turn": state.turn,
                            "timestamp": _now(),
                            "actor": "harness",
                            "action": "unpoison",
                            "target": other_id,
                            "detail": f"Cascade unpoison from {node_id}",
                        }
                    )

            # Reload doc to reflect unpoison changes
            doc = self.repo._load()
            node_data = doc["nodes"][node_id]

        # Increment dispatch sequence for this redispatch
        self._current_dispatch_seq += 1

        # Append the manager's note and increment retry_count before dispatch
        current_ctx = list(node_data.get("retry_context", []))
        current_ctx.append(
            {
                "turn": state.turn,
                "attempt": retry_count + 1,
                "manager_note": action.manager_note,
            }
        )
        self.repo.update_node(
            node_id,
            retry_count=retry_count + 1,
            retry_context=current_ctx,
        )

        # Log REDISPATCH chronicle entry
        self._log_chronicle_entry(
            node_id=node_id,
            actor="manager",
            action="redispatch",
            metadata={
                "role": role,
                "weight": weight,
                "previous_attempts": retry_count + 1,
                "clear_findings": getattr(action, "clear_findings", None),
            },
            state=state,
            dispatch_seq=self._current_dispatch_seq,
        )

        # Handle clear_findings if specified
        clear_findings = getattr(action, "clear_findings", None)
        if clear_findings:
            self._handle_clear_findings(node_id, clear_findings, state)

        # Generate session ID and compute attempt number
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "manager",
                "action": "redispatch",
                "target": node_id,
                "detail": (
                    f"Redispatching {role} [{weight}] "
                    f"(attempt {retry_count + 1}/{max_retries}): {action.manager_note}"
                ),
            }
        )

        # Generate session ID and compute attempt number
        session_id = str(uuid.uuid4())
        attempt_number = self.repo.count_sessions_for_node(node_id) + 1

        # Write session_start to log
        self.repo.append_session_log(
            node_id,
            {
                "type": "session_start",
                "session_id": session_id,
                "node_id": node_id,
                "role": role,
                "dispatch_turn": state.turn,
                "attempt_number": attempt_number,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Track active session
        self._active_sessions[node_id] = session_id

        # Create ShadowFS for this redispatch (same as DISPATCH)
        shadow = ShadowFS(self.repo._content_dir)
        self._current_shadow = shadow
        self._current_node_id = node_id

        # Log shadow creation event
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "shadow_created",
                "target": node_id,
                "detail": f"ShadowFS initialized for {node_id} redispatch",
            }
        )

        # Get exploration hints for warm redispatch
        exploration_hints = self.repo.get_exploration_hints(node_id)

        # Assemble context view — same as DISPATCH but retry_context is now on the node
        doc = self.repo._load()  # reload after update
        if role == "builder":
            ctx = builder_view(
                doc,
                node_id,
                exploration_hints=exploration_hints,
            )
        elif role == "test_author":
            ctx = assemble_test_author_view(doc, node_id)
        elif role == "analyst":
            ctx = analyst_view(doc, f"Analyse {node_id}", [node_id])
        else:
            ctx = builder_view(
                doc,
                node_id,
                exploration_hints=exploration_hints,
            )

        # Materialize file content into the view (line numbers, etc.)
        ctx_materialized = materialize_view(ctx, base_path=self.repo._content_dir)

        # Create tool call callback for session logging
        def on_tool_call(tool_name: str, args: dict, turn: int, intent: str | None = None) -> None:
            """Called after each tool execution to write to session log."""
            self.repo.append_session_log(
                node_id,
                {
                    "type": "tool_call",
                    "session_id": session_id,
                    "turn": turn,
                    "tool": tool_name,
                    "args": args,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "intent": intent,
                },
            )

        try:
            # Pass shadow to subagent hook for tool attachment
            result = self._subagent_hook(
                node_id,
                role,
                ctx_materialized,
                session_id=session_id,
                on_tool_call=on_tool_call,
                shadow=shadow,
            )
        except SubagentError as e:
            reason = SuspensionReason(type=SuspensionType.WILD, detail=str(e))
            self.repo.update_node(
                node_id,
                status=NodeStatus.SUSPENDED,
                suspension_reason=reason.model_dump(),
            )
            self._wild_counts[node_id] = self._wild_counts.get(node_id, 0) + 1

            # Write session_end for failed session
            self.repo.append_session_log(
                node_id,
                {
                    "type": "session_end",
                    "session_id": session_id,
                    "status": "failed",
                    "iterations_used": 0,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            del self._active_sessions[node_id]

            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "wild_suspension",
                    "target": node_id,
                    "detail": str(e),
                    "wild_count": self._wild_counts[node_id],
                    "wild_source": "subagent_error",
                }
            )
            if self._wild_counts[node_id] >= 2:
                self._checkpoint(state)
                raise HaltSignal(
                    f"Node '{node_id}' has been wild-suspended "
                    f"{self._wild_counts[node_id]} times this session. "
                    "Invariant: same node wild twice → HALT. Human must intervene."
                )
            return state

        # Determine session status based on result
        session_status = "completed"
        if result.status == NodeStatus.SUSPENDED:
            session_status = "suspended"
        elif result.status == NodeStatus.POISONED:
            session_status = "failed"

        # Write session_end with files_written for exploration_hints
        self.repo.append_session_log(
            node_id,
            {
                "type": "session_end",
                "session_id": session_id,
                "status": session_status,
                "iterations_used": getattr(result, "iterations_used", 0),
                "files_written": list(result.files.keys()) if result.files else [],
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        del self._active_sessions[node_id]

        # Add session_id to result for tracking
        result.session_id = session_id

        self._pending_returns[node_id] = result
        new_in_flight = list(state.in_flight) + [node_id]
        return _update_state(state, in_flight=new_in_flight)

    def _can_fast_path_ground(self, node_id: str) -> bool:
        """
        Check if node can skip agent dispatch and go straight to hard-path gate.

        Fast-path criteria:
        - Node status is 'provisional' (has tests, needs validation)
        - Implementation file {node_id}.py exists
        - Test file {node_id}.test exists
        """
        doc = self.repo._load()
        node = doc.get("nodes", {}).get(node_id, {})

        # Must be provisional (has tests but not yet grounded)
        if node.get("status") != "provisional":
            return False

        # Must have both implementation and test files
        impl_path = self.repo._content_dir / f"{node_id}.py"
        test_path = self.repo._content_dir / f"{node_id}.test"

        return impl_path.exists() and test_path.exists()

    def _try_fast_path_ground(self, node_id: str, state: ManagerState) -> FastPathResult:
        """
        Attempt to fast-path a provisional node to grounded by running tests directly.

        If tests pass: promote to grounded without agent involvement.
        If tests fail: return failure, caller should proceed with normal dispatch.
        """
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "fast_path_attempt",
                "target": node_id,
                "detail": "Provisional node has existing files, attempting fast-path validation",
            }
        )

        # Run hard-path gate directly
        test_path = self.repo._content_dir / f"{node_id}.test"
        test_script = test_path.read_text(encoding="utf-8")
        passed, detail = self._validator.run_hard_path(
            test_script,
            content_dir=str(self.repo._content_dir),
            test_path=str(test_path),
        )

        if passed:
            # Promote to grounded
            self.repo.update_node(
                node_id,
                status=NodeStatus.GROUNDED,
                pr_note="Fast-path grounded: existing implementation passed tests",
            )
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "fast_path_grounded",
                    "target": node_id,
                    "detail": "Existing implementation passed hard-path gate",
                }
            )
            # Log chronicle entry for the transition
            self._log_chronicle_entry(
                node_id=node_id,
                actor="agent",
                action="fast_path_commit",
                metadata={
                    "role": "builder",
                    "prior_status": "provisional",
                    "new_status": "grounded",
                    "files_written": [],
                },
                state=state,
                dispatch_seq=self._current_dispatch_seq,
            )
            return FastPathResult(success=True, state=state)
        else:
            # Tests failed, log and return failure
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "fast_path_failed",
                    "target": node_id,
                    "detail": detail,
                }
            )
            return FastPathResult(success=False, state=state, detail=f"Fast-path failed: {detail}")

    def _can_auto_validate(self, result, node_id: str) -> bool:
        """Skip manager VALIDATE turn for clean returns (provisional or grounded)."""
        if result.status not in (NodeStatus.PROVISIONAL, NodeStatus.GROUNDED):
            return False
        if result.escalate:
            return False
        if result.suspension_reason is not None:
            return False
        return True  # Schema validation elsewhere

    def _can_auto_commit(self, result, node_id: str) -> bool:
        """Skip manager COMMIT turn for verified grounded returns."""
        if result.status != NodeStatus.GROUNDED:
            return False
        if result.role != "builder":
            return False
        if result.escalate:
            return False
        if result.suspension_reason is not None:
            return False

        # Check test runs from result (populated by AgentLoop)
        runs = result.test_runs
        if not runs:
            return False

        return any(r.get("passed") for r in runs)

    # Phase 2: Fresh-Context Redispatch helper methods
    def _detect_modified_files(self, node_id: str, files_dict: dict[str, str] | None) -> list[str]:
        """Detect which files were modified vs previously committed."""
        if not files_dict:
            return []

        doc = self.repo._load()
        node = doc.get("nodes", {}).get(node_id, {})
        committed = set(node.get("committed_files", []))
        current = set(files_dict.keys())
        return list(current - committed)

    def _has_test_output(self, node_id: str) -> bool:
        """Check if there is test output available for this node."""
        # Check for test output in session logs or shadow
        # For now, check if test file exists
        test_path = self.repo._content_dir / f"{node_id}.test"
        return test_path.exists()

    def _get_test_output(self, node_id: str) -> str:
        """Get test output for a node (from most recent test run)."""
        # Try to get from shadow first, then from session logs
        # Fallback: return empty string
        return ""

    def _classify_dispatch_result(self, result) -> Any:
        """Classify dispatch result for redispatch guidance."""
        from a7_rt_core.context.core import ErrorClassification, TestOutputCompressor

        if result.suspension_reason:
            return TestOutputCompressor.classify_error(
                result.suspension_reason.detail,
                "UnknownError",
            )

        if result.test_runs:
            failed_runs = [r for r in result.test_runs if not r.get("passed")]
            if failed_runs:
                latest = failed_runs[-1]
                return ErrorClassification(
                    category="assertion",
                    severity="fixable",
                    affected_files=list(result.files.keys()) if result.files else [],
                    common_pattern="Test assertions failing",
                )

        return None

    # Abstract methods — implemented in other mixins
    def _checkpoint(self, state: ManagerState) -> None:
        raise NotImplementedError

    def _apply_return(
        self,
        node_id: str,
        result,
        override_status: NodeStatus,
        state: ManagerState,
    ) -> ManagerState:
        raise NotImplementedError
