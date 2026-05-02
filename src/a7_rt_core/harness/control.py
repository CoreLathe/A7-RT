"""
Harness lifecycle and orchestration mixin.

Provides: __init__, step(), init_session(), lifecycle management,
action routing, and turn advancement.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from a7_rt_core.context.core import BudgetExceeded, manager_view
from a7_rt_core.core.graph import cycle_check
from a7_rt_core.core.models import (
    CommitAction,
    ConsultAction,
    DispatchAction,
    HaltAction,
    ManagerAction,
    ManagerMode,
    ManagerState,
    NodeStatus,
    RedispatchAction,
    SealAction,
    SubagentError,
    SubagentReturn,
    SuspendAction,
    SuspensionReason,
    SuspensionType,
    UpdatePlanAction,
    ValidateAction,
)
from a7_rt_core.harness._common import _now, _update_state
from a7_rt_core.storage.repository import (
    InvariantViolation,
    Repository,
    RepositoryError,
)
from a7_rt_core.storage.shadowfs import ShadowFS
from a7_rt_core.validation.schema import Validator


class HaltSignal(Exception):
    """
    Raised when the harness encounters a condition that requires human review.
    Not a crash — the session is checkpointed before this propagates.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"[HALT] {reason}")


# Hook type aliases
ManagerHook = Callable[[dict, ManagerState], ManagerAction]
"""
Called with (board_view, manager_state) → ManagerAction.
The hook acts as the manager LLM: reads the board, returns one action.
"""

SubagentHook = Callable[[str, str, dict], SubagentReturn]
"""
Called with (node_id, role, context_view, *, session_id=None, on_tool_call=None, shadow=None) → SubagentReturn.
role: "builder" | "test_author" | "analyst"
The hook acts as a subagent LLM: does the work, returns structured result.
Additional kwargs: session_id (str), on_tool_call (callable), shadow (ShadowFS).
"""

ConsultHook = Callable[[str, list, list], Any]
"""
Called with (question, relevant_node_ids, constraints) → A7Verdict dict.
Optional — if not provided, CONSULT actions log and continue.
"""


class ControlMixin:
    """
    Lifecycle orchestration: session init, turn loop, action routing, lifecycle hooks.
    """

    def __init__(
        self,
        repo: Repository,
        manager_hook: ManagerHook,
        subagent_hook: SubagentHook,
        consult_hook: Optional[ConsultHook] = None,
        git_root: Optional[Path] = None,
        manager_budget: int = 8_000,
    ) -> None:
        self.repo = repo
        self._manager_hook = manager_hook
        self._subagent_hook = subagent_hook
        self._consult_hook = consult_hook
        self.git_root = git_root
        self.manager_budget = manager_budget

        self._paused = False
        self._pause_lock = threading.Lock()

        # Thread-safe human-input queue
        self._human_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        # Per-session pending state (not persisted — rebuilt on resume)
        # pending_returns: node_id → SubagentReturn awaiting VALIDATE
        # validated_returns: node_id → SubagentReturn ready for COMMIT
        # pending_consult: A7Verdict dict or None
        # _wild_counts: node_id → number of wild suspensions this session.
        #   Manager prompt invariant: same node wild twice → HALT.
        #   Enforced mechanically here so the manager cannot accidentally
        #   re-dispatch a repeatedly-failing node.
        self._pending_returns: dict[str, SubagentReturn] = {}
        self._validated_returns: dict[str, SubagentReturn] = {}
        self._pending_consult: Optional[dict] = None
        self._wild_counts: dict[str, int] = {}
        self._lifecycle: str = "new"  # "new" | "resumed"
        self._active_sessions: dict[str, str] = {}  # node_id -> session_id
        # Last harness-level error to surface on the next board (e.g. SEAL rejection).
        # Cleared after board assembly so the manager sees it exactly once.
        self._last_error: Optional[str] = None

        # One-round persistence tracking for CONSULT
        # Set when CONSULT is issued; prevents immediate clear on same turn
        self._consult_issued_this_turn: bool = False

        # ShadowFS tracking for agent dispatch (Phase 3: ShadowFS Integration)
        # _current_shadow: ShadowFS instance for active dispatch
        # _current_node_id: Node ID currently using shadow
        self._current_shadow: ShadowFS | None = None
        self._current_node_id: str | None = None

        # Fresh-Context Redispatch context (Phase 2)
        # _pending_redispatch_context: node_id -> PreviousAttemptSummary
        self._pending_redispatch_context: dict[str, Any] = {}

        # Validator instance — used for hard-path test execution on grounded COMMIT
        self._validator = Validator()

        # Chronicle tracking (Phase 6.5: Node Chronicle)
        # _current_dispatch_seq: increments on each DISPATCH/REDISPATCH
        # _current_manager_thought: captured from action for chronicle logging
        # _current_manager_intent: captured from action for chronicle logging
        self._current_dispatch_seq: int = 0
        self._current_manager_thought: str | None = None
        self._current_manager_intent: str | None = None

        # Rate limiting: inter-call delay tracking
        # _last_call_timestamp: epoch seconds of last LLM call
        # _inter_call_delay_ms: configured delay between API calls
        self._last_call_timestamp: float = 0.0
        self._inter_call_delay_ms: int = 0

    def step(self, state: ManagerState) -> ManagerState:
        """
        Execute one turn and return the updated ManagerState.

        Intended for TUI loop ownership: the TUI calls step() in its own
        event loop rather than yielding control to run_session().

        Raises HaltSignal on invariant violations or HALT actions.
        Returns state with mode=DEAD when the session ends (SEAL or max_turns).

        Callers must:
          1. Call run_session_init() first to get the initial state.
          2. Call step() each turn while not state.is_dead.
          3. Handle HaltSignal for human review.
        """
        if state.is_dead:
            raise HaltSignal("step() called on a dead manager")

        if self._paused:
            return state

        state = self._drain_events(state)

        doc = self.repo._load()
        try:
            board = manager_view(
                doc,
                self.manager_budget,
                state=state,
                pending_returns=self._pending_returns,
                pending_commits=self._validated_returns,
                pending_consult=self._pending_consult,
                lifecycle=self._lifecycle,
                last_error=self._last_error,
            )
        except BudgetExceeded as e:
            self._checkpoint(state)
            raise HaltSignal(str(e)) from e

        self._last_error = None

        # Check for test files based on node type
        doc = self.repo._load()
        for node_id, node_view in board.get("nodes", {}).items():
            node_type = doc.get("nodes", {}).get(node_id, {}).get("type", "feature")
            if node_type == "test":
                # Test-type nodes write {node_id}.py as their test module
                node_view["has_test"] = (self.repo._content_dir / f"{node_id}.py").exists()
            else:
                # Feature/glue nodes have {node_id}.test files
                node_view["has_test"] = (self.repo._content_dir / f"{node_id}.test").exists()

        cycles = cycle_check(doc.get("dependencies", []))
        if cycles:
            self._checkpoint(state)
            raise HaltSignal(f"Cycle detected in dependency graph: {cycles[0]}")

        # Fan-in auto-trigger: queue analyst audits for convergence points
        state = self._check_fan_in_triggers(doc, state)

        if state.is_drain:
            no_pending = (
                not self._pending_returns and not self._validated_returns and not state.in_flight
            )
            if no_pending:
                self.kill(state, "drain complete: all nodes resolved")
                return _update_state(state, mode=ManagerMode.DEAD)

        # Apply rate limiting delay before manager call
        self._apply_inter_call_delay()

        action = self._manager_hook(board, state)
        state = self._route_action(action, state)

        if state.is_dead:
            return state

        state = self._advance_turn(state)

        # One-round persistence for CONSULT: clear after manager has seen it.
        # Only clear if we didn't just issue a CONSULT this turn.
        if self._pending_consult is not None and not self._consult_issued_this_turn:
            self._pending_consult = None
        self._consult_issued_this_turn = False  # Reset for next turn
        if state.human_input_queue:
            state = _update_state(state, human_input_queue=[])
        self._checkpoint(state)

        if state.is_dead:
            self.kill(state, f"manager_max_turns={state.manager_max_turns} reached")

        return state

    def _resolve_stage_id(self, stage_id: str) -> str:
        """
        Resolve a stage identifier (name or ID) to the canonical stage ID.

        If stage_id matches a stage's name field but not a stage key,
        return the actual stage ID. Otherwise return stage_id unchanged.
        """
        doc = self.repo._load()
        stages = doc.get("stages", {})

        # If stage_id is already a valid stage key, use it
        if stage_id in stages:
            return stage_id

        # Otherwise, try to find a stage with matching name
        for sid, stage in stages.items():
            if stage.get("name") == stage_id:
                return sid

        # Not found - return as-is (will fail later with clear error)
        return stage_id

    def init_session(self, stage_id: str) -> ManagerState:
        """
        Initialise (or resume) an manager lifecycle and return the initial state.

        Call this once before the first step() call. run_session() calls this
        internally — use init_session() only when driving the loop via step().
        """
        # Resolve stage names (e.g., "stage-1") to actual stage IDs (UUIDs)
        stage_id = self._resolve_stage_id(stage_id)

        project = self.repo.load_project()
        existing = self.repo.load_manager_state()
        if existing is not None:
            state = existing
            self._lifecycle = "resumed"
            self._wild_counts = dict(state.wild_counts)
            # Rebuild _validated_returns from persisted pending_commits
            self._validated_returns = self._rebuild_validated_returns(state.pending_commits)

            # Derive current_dispatch_seq from max chronicle entry on resume
            doc = self.repo._load()
            max_seq = 0
            for node_id in doc.get("nodes", {}):
                node = doc["nodes"].get(node_id, {})
                metadata = node.get("metadata") or {}
                chronicle = metadata.get("chronicle", [])
                for entry in chronicle:
                    if entry.get("dispatch_seq"):
                        max_seq = max(max_seq, entry["dispatch_seq"])
            self._current_dispatch_seq = max_seq
            # Handle interrupted in-flight nodes: clear them so manager can re-dispatch
            # Session logs preserve exploration context for warm redispatch
            if state.in_flight:
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "resume_interrupt_recovery",
                        "target": state.current_stage_id,
                        "detail": f"Recovered from interrupt with in-flight nodes: {list(state.in_flight)}",
                    }
                )
                # Clear in_flight - manager will see these as ready and re-dispatch
                # with exploration hints from session logs
                state = _update_state(state, in_flight=[])

            # Mark orphaned sessions (no session_end) as interrupted
            # This can happen if the harness crashed mid-session
            doc = self.repo._load()
            for node_id in doc.get("nodes", {}):
                active_session = self.repo.get_active_session_for_node(node_id)
                if active_session:
                    self.repo.append_session_log(
                        node_id,
                        {
                            "type": "session_end",
                            "session_id": active_session.session_id,
                            "status": "interrupted",
                            "iterations_used": active_session.iterations_used or 0,
                            "ended_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    self.repo.append_event(
                        {
                            "turn": state.turn,
                            "timestamp": _now(),
                            "actor": "harness",
                            "action": "session_interrupted",
                            "target": node_id,
                            "detail": f"Session {active_session.session_id[:8]}... marked interrupted on resume",
                        }
                    )
        else:
            state = ManagerState(
                current_stage_id=stage_id,
                turn=0,
                manager_max_turns=project.manager_max_turns,
                drain_turn=project.drain_turn,
                mode=ManagerMode.AUTONOMOUS,
            )
            self._lifecycle = "new"

        self.repo.manager_checkpoint(state)
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "session_start",
                "target": stage_id,
                "detail": (
                    f"Lifecycle {self._lifecycle}. "
                    f"manager_max_turns={state.manager_max_turns}, drain_turn={state.drain_turn}"
                ),
            }
        )
        return state

    def enter_drain_mode(self, state: ManagerState) -> ManagerState:
        """
        Force the manager into drain mode immediately (before drain_turn).
        No new dispatches will be accepted. In-flight work continues to resolve.
        Thread-safe to call between turns.
        """
        if state.is_dead:
            return state
        new_state = _update_state(state, mode=ManagerMode.DRAIN)
        self._checkpoint(new_state)
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "enter_drain",
                "target": state.current_stage_id,
                "detail": "drain mode entered manually",
            }
        )
        return new_state

    def exit_drain_mode(self, state: ManagerState) -> ManagerState:
        """
        Return the manager to autonomous mode from drain.
        No-op if already autonomous or dead.
        Only valid before drain_turn — after that, _advance_turn() will re-enter drain.
        """
        if state.mode != ManagerMode.DRAIN:
            return state
        new_state = _update_state(state, mode=ManagerMode.AUTONOMOUS)
        self._checkpoint(new_state)
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "exit_drain",
                "target": state.current_stage_id,
                "detail": "drain mode exited manually",
            }
        )
        return new_state

    def halt(self, state: ManagerState, reason: str) -> None:
        """
        Checkpoint and raise HaltSignal immediately.
        Use for TUI-initiated halts (panic stop, user interrupt).
        """
        self._checkpoint(state)
        raise HaltSignal(reason)

    def run_session(self, stage_id: str) -> None:
        """
        Main loop. Runs until dead (max_turns reached or kill() called).
        Delegates to init_session() + step() so TUI can call those directly.
        """
        state = self.init_session(stage_id)

        while not state.is_dead:
            if self._paused:
                import time

                time.sleep(0.1)
                continue
            state = self.step(state)
            if state.is_dead:
                return

    def kill(self, state: ManagerState, reason: str) -> None:
        """
        Terminate the lifecycle: seal the manager, commit if git_root is set.
        Safe to call from any mode.
        """
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "kill",
                "target": state.current_stage_id,
                "detail": reason,
            }
        )
        self.repo.manager_seal(state)
        if self.git_root:
            self._git_commit(f"A7-RT: lifecycle end — {reason}")

    def pause(self) -> None:
        """Freeze the main loop between turns. Thread-safe."""
        with self._pause_lock:
            self._paused = True

    def resume(self) -> None:
        """Unfreeze the main loop. Thread-safe."""
        with self._pause_lock:
            self._paused = False

    def enqueue_human_input(self, event: dict[str, Any]) -> None:
        """
        Post a human-originated event for processing at the next turn boundary.
        Thread-safe — may be called from any thread.
        """
        self._human_queue.put_nowait(event)

    def _apply_inter_call_delay(self) -> None:
        """
        Apply rate limiting delay between API calls.

        Checks _inter_call_delay_ms and sleeps if needed to maintain
        the configured delay between LLM calls.
        """
        if self._inter_call_delay_ms <= 0:
            return

        now = time.time()
        elapsed_ms = (now - self._last_call_timestamp) * 1000
        remaining_ms = self._inter_call_delay_ms - elapsed_ms

        if remaining_ms > 0:
            time.sleep(remaining_ms / 1000.0)

        self._last_call_timestamp = time.time()

    def set_inter_call_delay(self, delay_ms: int) -> None:
        """Set the inter-call delay for rate limiting."""
        self._inter_call_delay_ms = max(0, delay_ms)

    def _advance_turn(self, state: ManagerState) -> ManagerState:
        """
        Increment turn counter; switch mode at thresholds.

          turn < drain_turn  → autonomous
          drain_turn ≤ turn < max_turns → drain
          turn == max_turns  → dead
        """
        new_turn = state.turn + 1
        if state.manager_max_turns is not None and new_turn >= state.manager_max_turns:
            new_mode = ManagerMode.DEAD
        elif state.drain_turn is not None and new_turn >= state.drain_turn:
            new_mode = ManagerMode.DRAIN
        else:
            new_mode = ManagerMode.AUTONOMOUS
        return _update_state(state, turn=new_turn, mode=new_mode)

    def _route_action(self, action: ManagerAction, state: ManagerState) -> ManagerState:
        """Dispatch an ManagerAction to the appropriate handler."""
        # Capture manager thought/intent for chronicle logging
        # These are cleared after being logged by _log_chronicle_entry
        self._current_manager_thought = getattr(action, "manager_note", None)
        self._current_manager_intent = getattr(action, "intent", None)

        if isinstance(action, RedispatchAction):
            return self._handle_redispatch(action, state)
        elif isinstance(action, DispatchAction):
            return self._handle_dispatch(action, state)
        elif isinstance(action, ValidateAction):
            return self._handle_validate(action, state)
        elif isinstance(action, CommitAction):
            return self._handle_commit(action, state)
        elif isinstance(action, ConsultAction):
            return self._handle_consult(action, state)
        elif isinstance(action, SuspendAction):
            return self._handle_suspend(action, state)
        elif isinstance(action, SealAction):
            return self._handle_seal(action, state)
        elif isinstance(action, UpdatePlanAction):
            return self._handle_update_plan(action, state)
        elif isinstance(action, HaltAction):
            self._checkpoint(state)
            raise HaltSignal(
                action.reason + (f" [invariant: {action.invariant}]" if action.invariant else "")
            )
        else:
            raise HaltSignal(f"Unknown action type: {type(action).__name__}")

    def _check_fan_in_triggers(self, doc: dict[str, Any], state: ManagerState) -> ManagerState:
        """
        Auto-trigger analyst audit for fan-in nodes before SEAL.

        Fan-in nodes (multiple upstream dependencies) are convergence points
        where interface inconsistencies can cause integration failures. This
        method queues analyst dispatches for these nodes automatically.

        Only triggers if:
            - Node is not already grounded/sealed/poisoned
            - No existing analyst_findings in metadata
            - Auto-trigger is enabled (future: configuration flag)
        """
        from a7_rt_core.core.graph import fan_in_candidates_for_audit

        nodes = doc.get("nodes", {})
        deps = doc.get("dependencies", [])

        # Find fan-in candidates (exclude near/provisional — nothing to analyze yet)
        candidates = fan_in_candidates_for_audit(
            nodes,
            deps,
            threshold=2,
            exclude_statuses={"grounded", "sealed", "poisoned", "near", "provisional"},
        )

        triggered = 0
        for candidate in candidates:
            nid = candidate["node_id"]
            node = nodes.get(nid)
            if not node:
                continue

            # Skip if already has analyst findings
            metadata = node.get("metadata") or {}
            if metadata.get("analyst_findings"):
                continue

            # Skip if already in human_input_queue
            queue = state.human_input_queue or []
            already_queued = any(
                e.get("action") == "analyst_escalation" and e.get("node_id") == nid for e in queue
            )
            if already_queued:
                continue

            # Queue analyst dispatch suggestion for manager
            suggestion = {
                "action": "analyst_escalation",
                "node_id": nid,
                "scope": "project",
                "query": candidate["suggested_query"],
                "target_nodes": candidate["target_nodes"],
                "source": "fan_in_auto_trigger",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "detail": (
                    f"Fan-in node {nid} has {candidate['prereq_count']} "
                    f"upstream dependencies. Suggest analyst audit before SEAL."
                ),
            }

            queue.append(suggestion)
            triggered += 1

            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "fan_in_trigger",
                    "target": nid,
                    "detail": (
                        f"Fan-in auto-trigger: {candidate['prereq_count']} "
                        f"upstream nodes ({', '.join(candidate['prereq_nodes'][:3])}"
                        f"{'...' if len(candidate['prereq_nodes']) > 3 else ''})"
                    ),
                }
            )

        if triggered > 0:
            return _update_state(state, human_input_queue=queue)
        return state

    def _handle_clear_findings(
        self, node_id: str, clear_findings: list[str], state: ManagerState
    ) -> None:
        """
        Clear analyst_findings for specified node IDs.

        Called when DISPATCH, REDISPATCH, or COMMIT actions include clear_findings.
        Removes findings where finding["node_id"] == target_id for each target.
        Logs event to events.jsonl with reason derived from action context.
        """
        if not clear_findings:
            return

        doc = self.repo._load()
        cleared_count = 0

        for target_id in clear_findings:
            # Find nodes that have findings for this target_id
            for nid, node in doc.get("nodes", {}).items():
                metadata = node.get("metadata") or {}
                findings = list(metadata.get("analyst_findings", []))

                # Filter out findings for the target_id
                new_findings = [f for f in findings if f.get("node_id") != target_id]

                if len(new_findings) < len(findings):
                    # Update metadata with cleared findings
                    metadata_update = dict(metadata)
                    metadata_update["analyst_findings"] = new_findings
                    self.repo.update_node(nid, metadata=metadata_update)
                    cleared_count += len(findings) - len(new_findings)

        # Log the clear event
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "clear_findings",
                "target": node_id,
                "detail": f"Cleared {cleared_count} findings for nodes: {clear_findings}",
                "cleared_for": clear_findings,
            }
        )

    def _log_chronicle_entry(
        self,
        node_id: str,
        actor: Literal["manager", "agent", "analyst"],
        action: str,
        metadata: dict,
        state: ManagerState,
        dispatch_seq: int | None = None,
    ) -> None:
        """
        Append a chronicle entry to the node's metadata.

        Chronicle is append-only process memory. Manager entries include
        thought/intent; agent entries include dispatch_seq for context tracking.
        """
        from a7_rt_core.core.models import ChronicleEntry, NodeMetadata

        entry = ChronicleEntry(
            turn=state.turn,
            actor=actor,
            action=action,
            metadata=metadata,
            manager_thought=self._current_manager_thought,
            manager_intent=self._current_manager_intent,
            dispatch_seq=dispatch_seq or self._current_dispatch_seq,
        )

        # Load current metadata
        doc = self.repo._load()
        node = doc.get("nodes", {}).get(node_id)
        if not node:
            return

        current_metadata = node.get("metadata") or {}

        # Build updated metadata with new chronicle entry
        metadata_update = dict(current_metadata)
        chronicle = list(metadata_update.get("chronicle", []))
        chronicle.append(entry.model_dump(mode="json"))
        metadata_update["chronicle"] = chronicle

        # Preserve other metadata fields if they exist
        if "tokens" in current_metadata:
            metadata_update["tokens"] = current_metadata["tokens"]
        if "lines" in current_metadata:
            metadata_update["lines"] = current_metadata["lines"]
        if "content_hash" in current_metadata:
            metadata_update["content_hash"] = current_metadata["content_hash"]
        if "first_export_preview" in current_metadata:
            metadata_update["first_export_preview"] = current_metadata["first_export_preview"]
        if "plumbing_summary" in current_metadata:
            metadata_update["plumbing_summary"] = current_metadata["plumbing_summary"]
        if "computed_at" in current_metadata:
            metadata_update["computed_at"] = current_metadata["computed_at"]
        if "iteration_history" in current_metadata:
            metadata_update["iteration_history"] = current_metadata["iteration_history"]
        if "avg_iterations" in current_metadata:
            metadata_update["avg_iterations"] = current_metadata["avg_iterations"]
        if "analyst_findings" in current_metadata:
            metadata_update["analyst_findings"] = current_metadata["analyst_findings"]

        # Update node with new metadata (chronicle is inside metadata)
        self.repo.update_node(node_id, metadata=metadata_update)

        # Clear captured manager fields after logging
        self._current_manager_thought = None
        self._current_manager_intent = None
