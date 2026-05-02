"""
ApplyMixin — PR application to disk, file writes, metadata, and hard-path gates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a7_rt_core.core.graph import cycle_check, poison_set
from a7_rt_core.core.models import (
    Dependency,
    DependencyType,
    ManagerState,
    NodeStatus,
    SuspensionReason,
    SuspensionType,
)
from a7_rt_core.harness._common import _now, _update_state
from a7_rt_core.storage.repository import InvariantViolation
from a7_rt_core.tools.editor import EditError

if TYPE_CHECKING:
    from a7_rt_core.core.models import SubagentReturn
    from a7_rt_core.harness.control import ControlMixin


class ApplyMixin:
    """
    Apply PR returns to disk: file writes, metadata computation,
    poison propagation, dependency updates, and hard-path test gates.
    """

    def _apply_return(
        self: "ControlMixin",
        node_id: str,
        result: "SubagentReturn",
        override_status: NodeStatus,
        state: ManagerState,
    ) -> ManagerState:
        """
        Write SubagentReturn effects to master.json and disk.
        Orchestrates file writes, hard-path validation, metadata, and poison propagation.
        """
        # Resolve files to write (ShadowFS takes precedence)
        files_to_write = self._resolve_files_to_write(node_id, result, state)

        # Validate conventions before any writes
        self._validate_conventions(node_id, files_to_write, state)

        # test_author: write test files only, don't advance status
        if result.role == "test_author":
            return self._write_test_author_files(node_id, files_to_write, result, state)

        # Run hard-path gate for grounded status
        if override_status == NodeStatus.GROUNDED:
            override_status = self._run_hard_path_gate(
                node_id, files_to_write, result, override_status, state
            )

        # Build and apply node updates
        updates = self._build_node_updates(node_id, result, override_status, state)
        prev_status = self._apply_node_update(node_id, updates, state)

        # Propagate poison if status is poisoned
        if override_status == NodeStatus.POISONED:
            self._apply_poison_propagation(node_id, state)

        # Apply line-edits if present
        if result.edits:
            self._apply_edits(node_id, result.edits, state)

        # Write files and compute metadata
        if files_to_write:
            self._write_committed_files(node_id, files_to_write, result, state)

        # Process file tags
        if result.file_tags:
            self._merge_file_tags(node_id, result.file_tags, result.role, state.turn)

        # Persist analyst findings
        if result.role == "analyst" and result.analysis_result:
            self._persist_analyst_findings(node_id, result, state)

        # Clean up shadow after successful commit
        self._cleanup_shadow(node_id)

        # Handle git commit for grounded nodes
        if override_status == NodeStatus.GROUNDED and self.git_root:
            self._git_commit(f"A7-RT: node {node_id} grounded (turn {state.turn})")

        return state

    def _resolve_files_to_write(
        self: "ControlMixin",
        node_id: str,
        result: "SubagentReturn",
        state: ManagerState,
    ) -> dict[str, str]:
        """Resolve files from ShadowFS or result, with deprecation warning."""
        files_to_write: dict[str, str] = {}

        # ShadowFS is authoritative
        if self._current_shadow and self._current_shadow.has_changes():
            files_to_write = self._current_shadow.get_writes()
            if result.files:
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "submit_pr_files_deprecated",
                        "target": node_id,
                        "detail": "result.files ignored - using shadow state",
                    }
                )
        elif result.files:
            files_to_write = dict(result.files)
        elif result.content is not None:
            # Legacy single-file path
            files_to_write = {node_id: result.content}

        # Normalize underscore variants to dot convention (e.g., types_core.py -> types.core.py)
        files_to_write = self._normalize_filenames(node_id, files_to_write)

        return files_to_write

    def _normalize_filenames(
        self: "ControlMixin",
        node_id: str,
        files_to_write: dict[str, str],
    ) -> dict[str, str]:
        """Normalize underscore variants of node filenames to dot convention.

        Converts files like 'types_core.py' -> 'types.core.py' or
        'types_core_test.py' -> 'types.core.test' when node_id is 'types.core'.
        Only normalizes files that start with the underscore variant of node_id.
        """
        if not files_to_write:
            return files_to_write

        underscore_id = node_id.replace(".", "_")
        if underscore_id == node_id:
            return files_to_write

        normalized: dict[str, str] = {}
        for path, content in files_to_write.items():
            # Check if path starts with underscore variant (not already normalized)
            if path.startswith(underscore_id) and not path.startswith(node_id):
                # Replace underscore prefix with dot prefix
                suffix = path[len(underscore_id) :]
                # Also normalize _test.py -> .test for test files
                if suffix == "_test.py":
                    suffix = ".test"
                new_path = node_id + suffix
                normalized[new_path] = content
            else:
                normalized[path] = content

        return normalized

    def _write_test_author_files(
        self: "ControlMixin",
        node_id: str,
        files_to_write: dict[str, str],
        result: "SubagentReturn",
        state: ManagerState,
    ) -> ManagerState:
        """Write test files for test_author role. Node status stays 'near'."""
        doc = self.repo._load()
        node_type = doc.get("nodes", {}).get(node_id, {}).get("type", "feature")

        # Extract test content from various possible keys
        test_content = (
            files_to_write.get(f"{node_id}.test")
            or files_to_write.get(f"{node_id}_test.py")
            or files_to_write.get(node_id)
            or result.content
        )

        if not test_content:
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "test_script_empty",
                    "target": node_id,
                    "detail": "test_author returned no test content",
                }
            )
            return state

        # Determine file path based on node type
        if node_type == "test":
            test_path = self.repo._content_dir / f"{node_id}.py"
        else:
            test_path = self.repo._content_dir / f"{node_id}.test"

        self.repo._atomic_write_content(test_path, test_content)
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "test_script_written",
                "target": node_id,
                "detail": f"test_author commit: {test_path.name} written, node stays near",
            }
        )

        # Persist test_contract from test_author for builder transit
        if result.test_contract:
            self.repo.update_node(node_id, test_contract=result.test_contract)
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "test_contract_stored",
                    "target": node_id,
                    "detail": f"test_contract persisted ({len(result.test_contract)} chars)",
                }
            )

        return state

    def _run_hard_path_gate(
        self: "ControlMixin",
        node_id: str,
        files_to_write: dict[str, str],
        result: "SubagentReturn",
        override_status: NodeStatus,
        state: ManagerState,
    ) -> NodeStatus:
        """
        Execute hard-path test gate. Returns possibly modified status.
        On test failure: status becomes POISONED.
        On missing test: status becomes PROVISIONAL.
        """
        if result.role == "analyst":
            return override_status

        if not files_to_write:
            # Allow grounded if implementation already exists on disk
            impl_path = self.repo._content_dir / f"{node_id}.py"
            if impl_path.exists() and impl_path.read_text(encoding="utf-8").strip():
                # Implementation exists - proceed to test gate
                pass
            else:
                # Builder claimed grounded but wrote no files and none exist - reject
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "hard_path_failed",
                        "target": node_id,
                        "detail": "Builder claimed grounded but files_to_write is empty and no implementation exists",
                    }
                )
                return NodeStatus.POISONED

        # Write files pre-gate for imports
        for rel_path, content in files_to_write.items():
            file_path = self.repo._content_dir / rel_path
            self.repo._atomic_write_content(file_path, content)

        # Ensure primary .py exists for import
        primary_py = files_to_write.get(f"{node_id}.py") or files_to_write.get(node_id)
        if primary_py and f"{node_id}.py" not in files_to_write:
            py_path = self.repo._content_dir / f"{node_id}.py"
            self.repo._atomic_write_content(py_path, primary_py)

        test_path = self.repo._content_dir / f"{node_id}.test"
        if not test_path.exists():
            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "hard_path_missing",
                    "target": node_id,
                    "detail": f"No test script at content/{node_id}.test — dispatch test_author before builder",
                }
            )
            return NodeStatus.PROVISIONAL

        # Run the test from the original test file path (preserves __file__ for imports)
        test_script = test_path.read_text(encoding="utf-8")
        passed, detail = self._validator.run_hard_path(
            test_script,
            content_dir=str(self.repo._content_dir),
            test_path=str(test_path),
        )

        if passed:
            return override_status

        # Test failed - poison and cleanup
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "hard_path_failed",
                "target": node_id,
                "detail": detail,
            }
        )

        # Remove pre-gate writes
        for rel_path in list(files_to_write.keys()) + [f"{node_id}.py"]:
            try:
                (self.repo._content_dir / rel_path).unlink()
            except FileNotFoundError:
                pass

        # Record retry context
        self._record_retry_failure(node_id, detail, state)
        return NodeStatus.POISONED

    def _record_retry_failure(
        self: "ControlMixin", node_id: str, failure_reason: str, state: ManagerState
    ) -> None:
        """Record retry failure in node metadata."""
        doc = self.repo._load()
        node_data = doc["nodes"][node_id]
        current_count = node_data.get("retry_count", 0)
        current_ctx = list(node_data.get("retry_context", []))
        current_ctx.append(
            {
                "turn": state.turn,
                "attempt": current_count + 1,
                "failure_reason": failure_reason,
            }
        )
        self.repo.update_node(
            node_id,
            retry_count=current_count + 1,
            retry_context=current_ctx,
        )

    def _build_node_updates(
        self: "ControlMixin",
        node_id: str,
        result: "SubagentReturn",
        override_status: NodeStatus,
        state: ManagerState,
    ) -> dict[str, Any]:
        """Build update dict for node based on result."""
        updates: dict[str, Any] = {"status": override_status}

        if override_status == NodeStatus.SUSPENDED:
            if result.suspension_reason is None:
                result.suspension_reason = SuspensionReason(
                    type=SuspensionType.NEAR,
                    detail="Subagent returned suspended without reason",
                )
            updates["suspension_reason"] = result.suspension_reason.model_dump()

        if result.interface_update:
            doc = self.repo._load()
            node = doc["nodes"][node_id]
            existing_iface = node.get("interface", {"exports": [], "assumptions": []})
            merged = {
                "exports": list(
                    dict.fromkeys(
                        existing_iface.get("exports", [])
                        + result.interface_update.get("exports", [])
                    )
                ),
                "assumptions": list(
                    dict.fromkeys(
                        existing_iface.get("assumptions", [])
                        + result.interface_update.get("assumptions", [])
                    )
                ),
                # Preserve raises and guarantees from original contract
                "raises": existing_iface.get("raises", []),
                "guarantees": existing_iface.get("guarantees", []),
            }
            updates["interface"] = merged

        if result.pr_note:
            updates["pr_note"] = result.pr_note

        if result.test_contract:
            updates["test_contract"] = result.test_contract

        return updates

    def _apply_node_update(
        self: "ControlMixin",
        node_id: str,
        updates: dict[str, Any],
        state: ManagerState,
    ) -> str:
        """Apply updates to node, returning previous status."""
        doc = self.repo._load()
        prev_status = doc["nodes"][node_id]["status"]
        new_status = updates.get("status")

        # Skip if status unchanged (avoid transition validation error)
        if new_status is not None and new_status.value == prev_status:
            updates.pop("status")

        try:
            self.repo.update_node(node_id, **updates)
        except (ValueError, InvariantViolation) as e:
            from harness.control import HaltSignal

            raise HaltSignal(f"Schema violation updating node '{node_id}': {e}") from e

        return prev_status

    def _apply_poison_propagation(self: "ControlMixin", node_id: str, state: ManagerState) -> None:
        """
        Propagate poison downstream: downstream of [☠] is [☠].
        Batched: single load, modify in memory, single save.
        """
        doc = self.repo._load()
        to_poison = poison_set(node_id, doc["nodes"], doc.get("dependencies", []))
        poisoned_any = False

        for downstream_id in sorted(to_poison):
            downstream = doc["nodes"].get(downstream_id, {})
            if downstream.get("status") != NodeStatus.POISONED.value:
                downstream["status"] = NodeStatus.POISONED.value
                if downstream.get("poisoned_by") is None:
                    downstream["poisoned_by"] = node_id
                poisoned_any = True
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "poison",
                        "target": downstream_id,
                        "detail": f"Poisoned by {node_id} (downstream propagation)",
                    }
                )

        if poisoned_any:
            self.repo._save(doc)

    def _write_committed_files(
        self: "ControlMixin",
        node_id: str,
        files_to_write: dict[str, str],
        result: "SubagentReturn",
        state: ManagerState,
    ) -> None:
        """Write all files, compute metadata, update node."""
        from harness.utils import _compute_metadata

        all_metadata: dict[str, Any] = {}
        primary_content: str | None = None
        primary_path: Any = None

        # Write all files and compute metadata
        for rel_path, content in files_to_write.items():
            file_path = self.repo._content_dir / rel_path
            self.repo._atomic_write_content(file_path, content)

            metadata = _compute_metadata(content, str(file_path))
            all_metadata[rel_path] = metadata.model_dump(mode="json")

            if rel_path == node_id or rel_path == f"{node_id}.py":
                primary_content = content
                primary_path = file_path

        # Fallback: use first file as primary
        if primary_content is None:
            first_path = list(files_to_write.keys())[0]
            primary_content = files_to_write[first_path]
            primary_path = self.repo._content_dir / first_path

        # Write importable .py if not present
        if f"{node_id}.py" not in files_to_write:
            py_path = self.repo._content_dir / f"{node_id}.py"
            self.repo._atomic_write_content(py_path, primary_content)
            metadata = _compute_metadata(primary_content, str(py_path))
            all_metadata[f"{node_id}.py"] = metadata.model_dump(mode="json")

        # Ensure primary path is the .py file (not .test) for feature/glue nodes
        # Builder should never write .test, but if they did, correct it
        py_path = self.repo._content_dir / f"{node_id}.py"
        if py_path.exists() and str(primary_path) != str(py_path):
            primary_path = py_path
            primary_content = py_path.read_text(encoding="utf-8")

        # Build primary metadata with telemetry
        primary_metadata = _compute_metadata(primary_content, str(primary_path))
        metadata_dict = self._build_metadata_with_telemetry(
            node_id, primary_metadata, result, state
        )

        # Update node with files and metadata
        self.repo.update_node(
            node_id,
            content_file=str(primary_path.relative_to(self.repo.root)),
            metadata=metadata_dict,
            metadata_map=all_metadata,
            committed_files=list(files_to_write.keys()),
        )

        # Log shadow commit event
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "shadow_committed",
                "target": node_id,
                "detail": f"Committed {len(files_to_write)} files",
            }
        )

    def _build_metadata_with_telemetry(
        self: "ControlMixin",
        node_id: str,
        primary_metadata: Any,
        result: "SubagentReturn",
        state: ManagerState,
    ) -> dict[str, Any]:
        """Merge metadata with iteration telemetry and preserve cross-role fields."""
        doc = self.repo._load()
        existing_metadata = doc["nodes"][node_id].get("metadata") or {}

        iterations_used = getattr(result, "iterations_used", 1)
        tool_usage = getattr(result, "tool_usage", {})

        # Rolling window of iteration history
        iteration_history = existing_metadata.get("iteration_history", [])
        iteration_history.append(iterations_used)
        iteration_history = iteration_history[-10:]

        avg_iterations = (
            sum(iteration_history) / len(iteration_history) if iteration_history else 0.0
        )

        metadata_dict = primary_metadata.model_dump(mode="json")
        metadata_dict["iteration_history"] = iteration_history
        metadata_dict["avg_iterations"] = round(avg_iterations, 2)
        metadata_dict["tool_usage"] = tool_usage

        # Preserve cross-role metadata (Phase 9.4 fix)
        metadata_dict["chronicle"] = existing_metadata.get("chronicle", [])
        if "analyst_findings" in existing_metadata:
            metadata_dict["analyst_findings"] = existing_metadata["analyst_findings"]

        # Log telemetry
        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "iteration_telemetry",
                "target": node_id,
                "detail": f"iterations={iterations_used}, avg={avg_iterations:.2f}",
            }
        )

        return metadata_dict

    def _merge_file_tags(
        self: "ControlMixin",
        node_id: str,
        file_tags: list[dict],
        role: str,
        turn: int,
    ) -> None:
        """Merge file tags into node.tags with deduplication."""
        from harness.utils import _merge_file_tags

        _merge_file_tags(self.repo, node_id, file_tags, role, turn)

    def _persist_analyst_findings(
        self: "ControlMixin",
        node_id: str,
        result: "SubagentReturn",
        state: ManagerState,
    ) -> None:
        """Persist analyst findings to node metadata and handle escalation.

        Note: Analyst findings are ephemeral (one-turn only). They persist
        long enough for the manager to see them on the next board, then are
        cleared. The chronicle entry provides permanent audit history.
        """
        analysis = result.analysis_result
        scope = analysis.get("scope", "node")
        target_nodes = analysis.get("target_nodes", [node_id])

        # Determine finding node_id based on scope
        if scope == "node":
            finding_node_id = target_nodes[0] if target_nodes else node_id
        else:
            finding_node_id = node_id

        findings_record = {
            "node_id": finding_node_id,
            "scope": scope,
            "findings": analysis.get("findings", []),
            "confidence": analysis.get("confidence", "void"),
            "sources": analysis.get("sources", []),
            "created_at": _now(),
        }

        # Replace (don't append) findings — ephemeral one-turn semantics
        doc = self.repo._load()
        existing_metadata = doc["nodes"][node_id].get("metadata") or {}
        metadata_update = dict(existing_metadata)
        # Clear prior findings, set only current — manager sees this once
        metadata_update["analyst_findings"] = [findings_record]
        self.repo.update_node(node_id, metadata=metadata_update)

        # Log finding
        self._log_chronicle_entry(
            node_id=node_id,
            actor="analyst",
            action="finding",
            metadata={
                "scope": findings_record["scope"],
                "confidence": findings_record["confidence"],
            },
            state=state,
        )

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "analyst_findings_recorded",
                "target": node_id,
                "detail": f"scope={scope}, confidence={findings_record['confidence']}",
            }
        )

        # Handle escalation — use fresh doc load to avoid stale data
        escalate = analysis.get("escalate", False)
        if escalate:
            escalation = {
                "action": "analyst_escalation",
                "node_id": node_id,
                "scope": scope,
                "findings": findings_record["findings"],
                "sources": findings_record["sources"],
                "recorded_at": findings_record["created_at"],
            }
            # Reload doc to get current state after update_node
            doc = self.repo._load()
            current_queue = doc.get("human_input_queue", [])
            current_queue.append(escalation)
            doc["human_input_queue"] = current_queue
            self.repo._save(doc)

            self.repo.append_event(
                {
                    "turn": state.turn,
                    "timestamp": _now(),
                    "actor": "harness",
                    "action": "analyst_escalation_queued",
                    "target": node_id,
                    "detail": "Escalation queued for human review",
                }
            )

    def _cleanup_shadow(self: "ControlMixin", node_id: str) -> None:
        """Clean up ShadowFS after successful commit."""
        if self._current_shadow and self._current_node_id == node_id:
            self._current_shadow.cleanup()
            self._current_shadow = None
            self._current_node_id = None

    def _apply_edits(
        self: "ControlMixin",
        node_id: str,
        edits: list[dict],
        state: ManagerState,
    ) -> None:
        """Apply line-addressed edit operations to existing files."""
        from a7_rt_core.tools.editor import EditOp

        # Group edits by filename
        edits_by_file: dict[str, list[dict]] = {}
        for edit in edits:
            filename = edit.get("filename", node_id)
            edits_by_file.setdefault(filename, []).append(edit)

        for filename, file_edits in edits_by_file.items():
            target_path = self.repo._content_dir / filename
            if not target_path.exists():
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "edit_failed",
                        "target": node_id,
                        "detail": f"Edit target not found: {filename}",
                    }
                )
                continue

            # Convert to EditOp format
            ops: list[EditOp] = []
            for edit in file_edits:
                op_type = edit.get("type")
                if op_type == "replace":
                    ops.append(
                        EditOp(
                            type="replace",
                            start=edit["start"],
                            end=edit["end"],
                            lines=edit.get("lines", []),
                        )
                    )
                elif op_type == "insert":
                    ops.append(
                        EditOp(
                            type="insert",
                            after=edit["after"],
                            lines=edit.get("lines", []),
                        )
                    )
                elif op_type == "delete":
                    ops.append(EditOp(type="delete", start=edit["start"], end=edit["end"]))

            try:
                result_text = self._editor.patch(filename, ops)
                self._editor.commit(filename, result_text)
                diff_text = self._editor.diff(filename, result_text)

                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "edits_applied",
                        "target": node_id,
                        "detail": f"Applied {len(ops)} edit(s) to {filename}",
                        "diff": diff_text if diff_text else None,
                    }
                )
            except EditError as e:
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "edit_failed",
                        "target": node_id,
                        "detail": f"Edit error in {filename}: {e}",
                    }
                )

    def _add_new_deps(
        self: "ControlMixin",
        node_id: str,
        result: "SubagentReturn",
        state: ManagerState,
        prev_status: str,
        override_status: NodeStatus,
    ) -> None:
        """Process new dependencies discovered by subagent."""
        for dep_dict in result.new_deps:
            dep = Dependency(
                from_node=dep_dict["from_node"],
                to_node=dep_dict["to_node"],
                type=DependencyType(dep_dict.get("type", "assumption")),
                verified=dep_dict.get("verified", False),
                discovered_by="subagent",
                turn=state.turn,
            )
            self.repo.add_dependency(dep)

            # Check for cycles
            doc = self.repo._load()
            cycles = cycle_check(doc.get("dependencies", []))
            if cycles:
                self._checkpoint(state)
                from harness.control import HaltSignal

                raise HaltSignal(
                    f"Cycle introduced by new dep {dep.from_node}→{dep.to_node}: {cycles[0]}"
                )

        self.repo.append_event(
            {
                "turn": state.turn,
                "timestamp": _now(),
                "actor": "harness",
                "action": "node_updated",
                "target": node_id,
                "detail": f"status → {override_status.value}",
                "state_delta": {node_id: f"{prev_status} → {override_status.value}"},
            }
        )
