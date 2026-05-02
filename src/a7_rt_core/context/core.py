"""
A7-RT Context Layer

Generates scoped views from master.json dicts for each LLM role.
No file I/O — Repository loads the doc, graph.py traverses it, this
module shapes it into role-appropriate context.

Role access matrix (what each role sees):
                        Manager  Builder  Test-Author  Analyst
  node status             yes      yes        yes         yes
  interface contract      yes      yes        yes         yes
  source (content_file)   NO       yes*       NO          yes
  dep source (struct)     NO       yes*       NO          yes
  dep interface only      NO       (assmp)    yes         --
  global tombstones       yes      yes        NO          yes
  local tombstones        NO       keyword    NO          all

  * content_file path is included; caller (subagent.py) reads the actual file.

Token counting: word-count proxy over json.dumps(view) with a 1.3x safety
margin (JSON punctuation undercounts raw word splits). Real tiktoken is a
later concern; the proxy is consistent and predictable. The only hard budget
enforcement is on the manager view — the spec says overflow is [HALT], not
truncation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from a7_rt_core.core.graph import ancestors, ready_nodes
from a7_rt_core.tools.agent_tools import _format_with_line_numbers

if TYPE_CHECKING:
    from a7_rt_core.core.models import ViewSpec

# ManagerState is imported lazily inside manager_view() to avoid a hard
# circular dep between context ← models during test bootstrap.

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

MasterDoc = dict[str, Any]  # deserialized master.json


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """
    Raised when an assembled view exceeds its token budget.
    Per spec: overflow is [HALT], not silent truncation.
    """

    def __init__(self, view_type: str, actual: int, budget: int) -> None:
        self.view_type = view_type
        self.actual = actual
        self.budget = budget
        super().__init__(
            f"[HALT] {view_type} view: {actual} tokens exceeds budget {budget}. "
            "Checkpoint and respawn with compressed context."
        )


# ---------------------------------------------------------------------------
# Redispatch Context Types (Phase 2: Rich Context Assembly)
# ---------------------------------------------------------------------------


@dataclass
class TestFailure:
    """Single compressed test failure."""

    test_name: str
    file_path: str
    line_number: int
    error_type: str  # AssertionError, AttributeError, etc.
    error_message: str  # Truncated to ~200 chars
    suggested_fix: str | None = None  # Optional harness suggestion


@dataclass
class ErrorClassification:
    """Harness classification of error patterns."""

    category: str  # "import", "syntax", "assertion", "type", "logic", "unknown"
    severity: str  # "blocking", "fixable", "warning"
    affected_files: list[str]
    common_pattern: str | None = None  # e.g., "Missing __init__.py in package"


@dataclass
class TestResultSummary:
    """Structured test result (not raw pytest output)."""

    passed: bool
    failure_count: int
    error_count: int
    key_failures: list[TestFailure]  # First N failures, compressed
    import_errors: list[str]  # Module-level import failures
    syntax_errors: list[str]  # Files with syntax errors


@dataclass
class PreviousAttemptSummary:
    """Summary of a previous dispatch attempt for warm redispatch context."""

    dispatch_number: int
    files_written: list[str]  # Paths written in previous attempt
    files_modified: list[str]  # Paths that changed
    test_result: TestResultSummary | None  # Structured test outcome
    thoughts: list[dict]  # Recorded thoughts from previous attempt
    final_status: str  # "suspended", "failed_validation", "checkpoint"
    error_classification: ErrorClassification | None  # Harness-classified error


class TestOutputCompressor:
    """Compress raw pytest output to structured, token-efficient summary."""

    @staticmethod
    def compress(raw_output: str, max_failures: int = 3) -> TestResultSummary:
        """
        Parse pytest output and extract structured failure information.

        Reduces 10K token pytest output to ~500 token structured summary.
        """
        passed = "passed" in raw_output.lower() and "failed" not in raw_output.lower()
        failure_count = 0
        error_count = 0
        key_failures: list[TestFailure] = []
        import_errors: list[str] = []
        syntax_errors: list[str] = []

        # Parse summary line: "X passed, Y failed, Z error"
        summary_match = re.search(
            r"(\d+) passed(?:,\s*(\d+) failed)?(?:,\s*(\d+) error)?",
            raw_output,
            re.IGNORECASE,
        )
        if summary_match:
            failure_count = int(summary_match.group(2) or 0)
            error_count = int(summary_match.group(3) or 0)
            passed = failure_count == 0 and error_count == 0

        # Extract failures
        # Pattern: "FAILED file::test_name" or "ERROR file::test_name"
        failure_pattern = re.compile(
            r"(FAILED|ERROR)\s+([\w/\\.]+)::(\w+)",
            re.MULTILINE,
        )
        failures_found = failure_pattern.findall(raw_output)

        for i, (status, file_path, test_name) in enumerate(failures_found[:max_failures]):
            # Find error details after this failure
            error_type = "AssertionError"
            error_message = ""
            line_number = 0

            # Look for the error details section
            search_start = raw_output.find(f"{status} {file_path}::{test_name}")
            if search_start != -1:
                # Look for error type and message
                error_section = raw_output[search_start : search_start + 2000]

                # Extract line number from traceback
                line_match = re.search(r"File\s+[^\n]+,\s+line\s+(\d+)", error_section)
                if line_match:
                    line_number = int(line_match.group(1))

                # Extract error type and message
                error_match = re.search(
                    r"(\w+Error):\s*(.+?)(?=\n\n|\n_+|\Z)",
                    error_section,
                    re.DOTALL,
                )
                if error_match:
                    error_type = error_match.group(1)
                    error_message = error_match.group(2)[:200].strip().replace("\n", " ")

            # Detect import errors
            if error_type in ("ImportError", "ModuleNotFoundError"):
                import_errors.append(file_path)

            # Detect syntax errors
            if error_type == "SyntaxError":
                syntax_errors.append(file_path)

            # Generate suggestion
            suggested_fix = TestOutputCompressor._generate_suggestion(
                error_type, error_message, file_path
            )

            key_failures.append(
                TestFailure(
                    test_name=test_name,
                    file_path=file_path,
                    line_number=line_number,
                    error_type=error_type,
                    error_message=error_message,
                    suggested_fix=suggested_fix,
                )
            )

        return TestResultSummary(
            passed=passed,
            failure_count=failure_count,
            error_count=error_count,
            key_failures=key_failures,
            import_errors=import_errors,
            syntax_errors=syntax_errors,
        )

    @staticmethod
    def _generate_suggestion(error_type: str, error_message: str, file_path: str) -> str | None:
        """Generate targeted suggestion based on error pattern."""
        suggestions = {
            "ImportError": f"Check imports in {file_path}. Missing dependency or circular import?",
            "ModuleNotFoundError": f"Module not found in {file_path}. Check __init__.py or dependency.",
            "SyntaxError": f"Fix syntax error in {file_path} before logic.",
            "AttributeError": "Check class definition matches contract. Missing attribute?",
            "AssertionError": "Implementation doesn't match test expectation. Review contract.",
            "TypeError": "Check function signatures match contract. Wrong argument types?",
            "NameError": f"Undefined name in {file_path}. Check imports and spelling.",
        }
        return suggestions.get(error_type)

    @staticmethod
    def classify_error(error_message: str, error_type: str) -> ErrorClassification:
        """Classify error pattern for targeted redispatch guidance."""
        category_map = {
            "ImportError": "import",
            "ModuleNotFoundError": "import",
            "SyntaxError": "syntax",
            "AttributeError": "type",
            "TypeError": "type",
            "AssertionError": "assertion",
            "NameError": "logic",
        }
        category = category_map.get(error_type, "unknown")

        # Determine severity
        severity = "blocking" if category in ("syntax", "import") else "fixable"

        # Extract affected files from error message
        affected_files = []
        file_matches = re.findall(r'File\s+"([^"]+)"', error_message)
        affected_files.extend(file_matches)

        # Detect common patterns
        patterns = {
            "import": "Missing __init__.py in package or import path issue",
            "syntax": "Fix syntax error before proceeding with logic",
            "assertion": "Review contract exports and test expectations",
        }
        common_pattern = patterns.get(category)

        return ErrorClassification(
            category=category,
            severity=severity,
            affected_files=affected_files,
            common_pattern=common_pattern,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_tokens(obj: Any) -> int:
    """
    Word-count proxy for token estimation with a 1.3x safety margin.

    JSON encoding adds quotes, colons, braces, and brackets that tiktoken
    tokenizes individually — the raw word count undercounts. The 1.3x factor
    compensates without requiring a tiktoken dependency. Consistent and fast.
    """
    return int(len(json.dumps(obj, default=str).split()) * 1.3)


def _active_stage(doc: MasterDoc, stage_id: str | None = None) -> dict | None:
    """Return the active stage dict.

    If stage_id is provided, returns that specific stage.
    Otherwise returns the first active stage.
    """
    stages = doc.get("stages", {})
    if stage_id and stage_id in stages:
        return stages[stage_id]
    return next(
        (s for s in stages.values() if s.get("status") == "active"),
        None,
    )


def _interface_only(node: dict) -> dict:
    """Strip a node dict down to interface-level information only (no content_file)."""
    return {
        "node_id": node["node_id"],
        "type": node.get("type"),
        "status": node.get("status"),
        "description": node.get("description"),
        "protocol_weight": node.get("protocol_weight"),
        "interface": node.get(
            "interface",
            {"exports": [], "assumptions": [], "raises": None, "guarantees": None},
        ),
        "structural_deps": node.get("structural_deps", []),
        "assumption_deps": node.get("assumption_deps", []),
        "suspension_reason": node.get("suspension_reason"),
        "poisoned_by": node.get("poisoned_by"),
        # content_file intentionally absent
    }


def _node_summary(node: dict, all_nodes: dict) -> dict:
    """
    Return a summary view of a node for the manager board.

    Includes metadata (tokens, exports_preview), blocking count
    (number of structural deps that are not grounded/provisional),
    and analyst_findings if available.
    """
    summary = {
        "node_id": node["node_id"],
        "status": node.get("status"),
        "type": node.get("type"),
        "description": node.get("description"),
        "interface": node.get(
            "interface",
            {"exports": [], "assumptions": [], "raises": None, "guarantees": None},
        ),
        "committed_files": node.get("committed_files", []),
        "poisoned_by": node.get("poisoned_by"),
    }

    # Add metadata if available
    metadata = node.get("metadata")
    if metadata:
        summary["tokens"] = metadata.get("tokens")
        summary["exports_preview"] = metadata.get("first_export_preview")
        # Include analyst_findings for manager awareness
        if metadata.get("analyst_findings"):
            findings_list = metadata["analyst_findings"]
            # Handle both old dict format and new list format
            if isinstance(findings_list, list) and findings_list:
                # New list format - take the most recent finding
                findings = findings_list[-1]
                summary["analyst_findings"] = {
                    "scope": findings.get("scope"),
                    "confidence": findings.get("confidence"),
                    "summary": (
                        findings.get("findings")[0]
                        if isinstance(findings.get("findings"), list) and findings.get("findings")
                        else str(findings.get("findings", ""))[:100]
                    ),
                    "count": len(findings_list),
                    "recorded_at": findings.get("created_at"),
                }
            elif isinstance(findings_list, dict):
                # Legacy dict format
                findings = findings_list
                summary["analyst_findings"] = {
                    "scope": findings.get("scope"),
                    "confidence": findings.get("confidence"),
                    "summary": findings.get("findings", {}).get("summary"),
                    "escalate": findings.get("escalate", False),
                    "recorded_at": findings.get("recorded_at"),
                }
    else:
        summary["tokens"] = None
        summary["exports_preview"] = None

    # Compute blocking count: structural deps that block dispatch
    struct_deps = node.get("structural_deps", [])
    blocking = 0
    _DISPATCHABLE = {"grounded", "provisional"}
    for dep_id in struct_deps:
        dep = all_nodes.get(dep_id)
        if dep and dep.get("status") not in _DISPATCHABLE:
            blocking += 1

    summary["blocking_count"] = blocking
    summary["structural_dep_count"] = len(struct_deps)

    return summary


def _one_line_summary(node: dict) -> str:
    """One-line interface summary for deep ancestors (> depth 2)."""
    exports = node.get("interface", {}).get("exports", [])
    first = exports[0] if exports else "(no exports declared)"
    return f"{node['node_id']} [{node.get('status', '?')}]: {first}"


def _compute_peer_patterns(doc: MasterDoc, node_id: str) -> dict[str, Any] | None:
    """
    Compute file organization patterns from peer nodes with the same prefix.

    Analyzes committed_files of grounded/provisional nodes that share the same
    prefix (e.g., 'utils.errors' and 'utils.validation' both have prefix 'utils').
    Returns pattern hints to guide file location decisions.

    Example:
        If 'utils.errors' has files ['utils/__init__.py', 'utils/errors.py'],
        then 'utils.validation' should follow the same pattern.
    """
    all_nodes = doc.get("nodes", {})
    target_node = all_nodes.get(node_id)
    if not target_node:
        return None

    # Extract prefix (e.g., 'utils.validation' -> 'utils')
    parts = node_id.split(".")
    if len(parts) < 2:
        return None  # No prefix to match

    prefix = parts[0]

    patterns: dict[str, dict[str, Any]] = {}

    for nid, node in all_nodes.items():
        if nid == node_id:
            continue
        if not nid.startswith(prefix + "."):
            continue  # Different prefix

        status = node.get("status")
        if status not in ("grounded", "provisional"):
            continue  # Only consider established nodes

        committed_files = node.get("committed_files", [])
        # Filter out test files - builders must NOT see *.test files
        committed_files = [f for f in committed_files if not f.endswith(".test")]
        if not committed_files:
            continue

        # Categorize the pattern
        has_package_init = any(f.endswith("/__init__.py") for f in committed_files)
        has_nested_module = any(
            "/" in f and not f.endswith("/__init__.py") for f in committed_files
        )

        pattern_key: str = (
            "package" if has_package_init else ("nested" if has_nested_module else "flat")
        )
        patterns[nid] = {
            "files": committed_files,
            "pattern_type": pattern_key,
            "has_package_init": has_package_init,
        }

    if not patterns:
        return None

    # Determine dominant pattern
    package_count = sum(1 for p in patterns.values() if p["pattern_type"] == "package")
    nested_count = sum(1 for p in patterns.values() if p["pattern_type"] == "nested")

    # Build recommendation based on dominant pattern
    if package_count > 0:
        recommendation = (
            f"Follow package structure: write to {prefix}/{'.'.join(parts[1:])}.py "
            f"and ensure {prefix}/__init__.py exists"
        )
        dominant = "package"
    elif nested_count > 0:
        recommendation = (
            f"Follow nested structure: write to {prefix}/{'.'.join(parts[1:])}.py "
            f"(create {prefix}/ directory if needed)"
        )
        dominant = "nested"
    else:
        recommendation = None
        dominant = "flat" if patterns else "unknown"

    return {
        "peers": patterns,
        "dominant_pattern": dominant,
        "recommendation": recommendation,
    }


def _keyword_overlap(text_a: str, text_b: str) -> bool:
    """
    True if the two texts share at least one meaningful content word.
    Used for tombstone relevance matching.
    """
    _STOP = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "of",
        "to",
        "in",
        "and",
        "or",
        "with",
        "by",
        "for",
        "on",
        "at",
        "from",
        "that",
        "this",
        "it",
        "its",
        "be",
        "has",
        "have",
        "had",
    }

    def words(text: str) -> set[str]:
        return {
            w.lower().strip(".,;:()[]")
            for w in text.split()
            if len(w) > 3 and w.lower() not in _STOP
        }

    return bool(words(text_a) & words(text_b))


def _node_search_text(node: dict) -> str:
    """Concatenate a node's description and interface fields into one string for keyword search."""
    iface = node.get("interface", {})
    parts = [
        node.get("description", ""),
        *iface.get("exports", []),
        *iface.get("assumptions", []),
    ]
    return " ".join(parts)


def _relevant_tombstones(doc: MasterDoc, node: dict, *, global_only: bool = False) -> list[dict]:
    """
    Return tombstones relevant to *node*.

    Always includes global tombstones (propagate across stage boundaries).
    Includes local tombstones from the same stage only when global_only=False
    and the tombstone has keyword overlap with the node's description/interface.
    """
    graveyard = doc.get("graveyard", [])
    node_text = _node_search_text(node)
    result = []
    for entry in graveyard:
        scope = entry.get("scope", "local")
        if scope == "global":
            result.append(entry)
        elif not global_only and scope == "local":
            if _keyword_overlap(entry.get("reason", ""), node_text):
                result.append(entry)
    return result


def _deps_in_set(doc: MasterDoc, node_id_set: set[str]) -> list[dict]:
    """Return dependency edges where both from_node and to_node are in node_id_set."""
    return [
        d
        for d in doc.get("dependencies", [])
        if d.get("from_node") in node_id_set and d.get("to_node") in node_id_set
    ]


# ---------------------------------------------------------------------------
# Public views
# ---------------------------------------------------------------------------


def _get_recent_events(repo: Any, window: int | None = None) -> list[dict]:
    """
    Read recent strategic events from events.jsonl.
    Filters to strategic event types only.
    Returns last N events, oldest first.
    """
    strategic_actions = {
        "dispatch",
        "commit",
        "suspend",
        "halt",
        "wild_suspension",
        "redispatch",
        "update_plan",
        "seal",
        "poison",
        "hard_path_missing",
    }
    try:
        # Get all events and filter to strategic ones
        events = repo.get_events()
        filtered = [
            {
                "turn": e.get("turn"),
                "action": e.get("action"),
                "target": e.get("target"),
                "detail": e.get("detail"),
            }
            for e in events
            if e.get("action") in strategic_actions
        ]
        # Return last N, oldest first (or all if window is None)
        if window is None:
            result = filtered
        else:
            result = filtered[-window:] if len(filtered) > window else filtered
        return list(reversed(result)) if result else []
    except Exception:
        return []


def manager_view(
    doc: MasterDoc,
    budget_tokens: int,
    *,
    repo: Any = None,
    state: Any = None,
    pending_returns: "dict[str, Any] | None" = None,
    pending_commits: "dict[str, Any] | None" = None,
    pending_consult: "dict | None" = None,
    lifecycle: str = "new",
    last_error: "str | None" = None,
) -> dict:
    """
    Assemble the manager's view of the current active stage.

    Contains: node statuses, interface contracts, dependency edges,
    ready-to-dispatch list, global tombstones, and board metadata
    (turn, mode, drain_turn, manager_max_turns, in_flight, context_pressure,
    pending_consult, pending_returns, lifecycle).

    Does NOT contain: source code, content_file references, or local tombstones.

    Parameters
    ----------
    doc             : master.json as dict
    budget_tokens   : hard cap; overflow raises BudgetExceeded
    state           : ManagerState (optional; provides turn/mode/in_flight)
    pending_returns : node_id → raw SubagentReturn waiting for VALIDATE
    pending_consult : A7 verdict dict from last CONSULT (None if none pending)
    lifecycle       : "new" | "resumed" — communicated to the manager

    Raises BudgetExceeded if the assembled view exceeds budget_tokens.
    Per spec: overflow is [HALT], not truncation — the harness must checkpoint
    and respawn the manager with a compressed context.
    """
    turn = state.turn if state is not None else 0
    manager_max_turns = (
        state.manager_max_turns
        if state is not None
        else doc.get("project", {}).get("manager_max_turns", 25)
    )
    drain_turn = (
        state.drain_turn if state is not None else doc.get("project", {}).get("drain_turn", 20)
    )
    mode = state.mode.value if state is not None else "autonomous"
    in_flight = list(state.in_flight) if state is not None else []

    # Resume notice: in_flight nodes from the prior lifecycle are recorded in
    # manager.json but their SubagentReturns are not persisted to disk.
    # On resume, pending_returns is always empty — the manager should re-dispatch
    # in-flight nodes directly (warm redispatch). Session logs preserve exploration
    # context from the interrupted attempt. Do not SUSPEND unless the node is
    # genuinely blocked.
    # Note: Also check pending_commits — validated nodes awaiting commit are handled.
    resume_warning: str | None = None
    has_pending_work = pending_returns or pending_commits
    if lifecycle == "resumed" and in_flight and not has_pending_work:
        resume_warning = (
            f"Nodes {in_flight} were in-flight when the prior lifecycle ended. "
            "Their subagent returns were not persisted, but session logs contain "
            "exploration context. DISPATCH these nodes directly — exploration hints "
            "will be provided automatically. Do not SUSPEND unless genuinely blocked."
        )

    # Use state.current_stage_id if available, else fall back to first active
    current_stage_id = state.current_stage_id if state is not None else None
    stage = _active_stage(doc, current_stage_id)
    if stage is None:
        sealed_stages_empty = [
            {
                "stage_id": s["stage_id"],
                "name": s["name"],
                "sealed_at": s.get("sealed_at"),
                "summary": s.get("summary"),
                "exported_interfaces": s.get("exported_interfaces", {}),
            }
            for s in doc.get("stages", {}).values()
            if s.get("status") == "sealed"
        ]
        view = {
            "lifecycle": lifecycle,
            "turn": turn,
            "manager_max_turns": manager_max_turns,
            "drain_turn": drain_turn,
            "mode": mode,
            "in_flight": in_flight,
            "active_stage": None,
            "nodes": {},
            "dependencies": [],
            "ready": [],
            "global_tombstones": [],
            "sealed_stages": sealed_stages_empty,
            "planning": state.planning.model_dump(mode="json")
            if state is not None and state.planning
            else None,
            "recent_events": _get_recent_events(repo) if repo else [],
            "pending_returns": {},
            "pending_commits": {},
            "pending_consult": pending_consult,
            "last_error": last_error,
            "resume_warning": resume_warning,
        }
        actual = _count_tokens(view)
        view["context_pressure"] = round(actual / budget_tokens, 3) if budget_tokens else 1.0
        return view

    node_id_set: set[str] = set(stage.get("node_ids", []))
    all_nodes = doc.get("nodes", {})

    # Node summaries with metadata — manager gets spatial awareness
    # Full detail only for in-flight and pending nodes; summaries for rest
    in_flight_and_pending = set(in_flight) | set(pending_returns or {}) | set(pending_commits or {})

    # Status ordering for board display (in_flight last for attention)
    _STATUS_ORDER = {
        "suspended": 0,
        "provisional": 1,
        "near": 2,
        "grounded": 3,
        "sealed": 4,
        "in_flight": 5,
    }

    def _make_node_view(nid: str) -> dict:
        node = all_nodes[nid]
        if nid in in_flight_and_pending:
            # Full interface view for active nodes + chronicle
            view = _interface_only(node)
            # Include chronicle for active nodes (last complete dispatch cycle)
            metadata = node.get("metadata") or {}
            chronicle = metadata.get("chronicle", [])
            if chronicle:
                # Find last complete dispatch cycle
                last_dispatch_idx = None
                for i, entry in enumerate(reversed(chronicle)):
                    if entry.get("action") in ["dispatch", "redispatch"]:
                        last_dispatch_idx = len(chronicle) - 1 - i
                        break
                if last_dispatch_idx is not None:
                    # Include entries from last dispatch onward
                    view["chronicle"] = chronicle[last_dispatch_idx:]
            # Include analyst_findings for active nodes
            analyst_findings = metadata.get("analyst_findings")
            if analyst_findings:
                view["analyst_findings"] = analyst_findings
            return view
        else:
            # Summary view for background nodes (no chronicle)
            return _node_summary(node, all_nodes)

    # Sort node_ids by status priority (in_flight last)
    def _status_sort_key(nid: str) -> tuple:
        node = all_nodes.get(nid, {})
        status = node.get("status", "near")
        # in_flight nodes get highest priority (sorted last)
        if nid in in_flight:
            priority = _STATUS_ORDER["in_flight"]
        else:
            priority = _STATUS_ORDER.get(status, 2)
        return (priority, nid)

    sorted_node_ids = sorted(node_id_set, key=_status_sort_key)

    nodes_view = {nid: _make_node_view(nid) for nid in sorted_node_ids if nid in all_nodes}

    # Dependency edges within the active stage
    stage_deps = _deps_in_set(doc, node_id_set)

    # Cross-stage tombstones only — local ones are invisible to manager
    global_tombstones = [e for e in doc.get("graveyard", []) if e.get("scope") == "global"]

    # Sealed stages: summary + exported interfaces only — no node details, no source
    sealed_stages = [
        {
            "stage_id": s["stage_id"],
            "name": s["name"],
            "sealed_at": s.get("sealed_at"),
            "summary": s.get("summary"),
            "exported_interfaces": s.get("exported_interfaces", {}),
        }
        for s in doc.get("stages", {}).values()
        if s.get("status") == "sealed"
    ]

    # Ready nodes (dispatchable) — computed over ALL nodes so cross-stage
    # grounded deps resolve correctly, then filtered to active stage
    all_ready = ready_nodes(all_nodes, doc.get("dependencies", []))
    ready = [nid for nid in all_ready if nid in node_id_set]

    # pending_returns: expose node_id, role, validation_status, and escalate flag
    # (not raw content) so the manager knows which nodes are awaiting VALIDATE
    # and which have requested escalation to a higher protocol weight.
    returns_summary = {
        nid: {
            "node_id": nid,
            "awaiting": "validate",
            "role": getattr(ret, "role", "builder"),
            "escalate": getattr(ret, "escalate", False),
            "pr_note": getattr(ret, "pr_note", None),
        }
        for nid, ret in (pending_returns or {}).items()
    }

    # pending_commits: nodes that have passed VALIDATE and are awaiting COMMIT.
    # The manager must COMMIT these before dispatching new work.
    # role is included so the manager can apply test_author COMMIT semantics
    # (status is ignored for test_author; node stays near regardless).
    commits_summary = {
        nid: {
            "node_id": nid,
            "awaiting": "commit",
            "role": getattr(ret, "role", "builder"),
            "status": getattr(ret, "status", None) and ret.status.value,
            "pr_note": getattr(ret, "pr_note", None),
        }
        for nid, ret in (pending_commits or {}).items()
    }

    # context_pressure: fraction of budget consumed by this view (computed last)
    # Enumerated valid targets — prevents manager hallucination
    # Includes ready nodes + poisoned roots (poisoned_by: null) + any in-flight or pending nodes that are not suspended
    poisoned_roots = [
        nid
        for nid in node_id_set
        if all_nodes.get(nid, {}).get("status") == "poisoned"
        and all_nodes.get(nid, {}).get("poisoned_by") is None
    ]
    valid_targets = (
        list(ready)
        + poisoned_roots
        + [
            nid
            for nid in in_flight_and_pending
            if nid in node_id_set and all_nodes.get(nid, {}).get("status") != "suspended"
        ]
    )

    view = {
        "lifecycle": lifecycle,
        "turn": turn,
        "manager_max_turns": manager_max_turns,
        "drain_turn": drain_turn,
        "mode": mode,
        "in_flight": in_flight,
        "active_stage": {
            "stage_id": stage["stage_id"],
            "name": stage["name"],
            "status": stage["status"],
        },
        "nodes": nodes_view,
        "dependencies": stage_deps,
        "ready": ready,
        "valid_dispatch_targets": valid_targets,
        "global_tombstones": global_tombstones,
        "sealed_stages": sealed_stages,
        "planning": state.planning.model_dump(mode="json")
        if state is not None and state.planning
        else None,
        "recent_events": _get_recent_events(repo) if repo else [],
        "pending_returns": returns_summary,
        "pending_commits": commits_summary,
        "pending_consult": pending_consult,
        "human_input": list(state.human_input_queue)
        if state is not None and state.human_input_queue
        else [],
        "last_error": last_error,
        "resume_warning": resume_warning,
    }

    actual = _count_tokens(view)
    # context_pressure inserted after token count so it doesn't inflate the count
    view["context_pressure"] = round(actual / budget_tokens, 3) if budget_tokens else 1.0

    # Token budget breakdown for manager spatial awareness
    view["token_budget"] = {
        "allocated": budget_tokens,
        "consumed": actual,
        "nodes_count": len(nodes_view),
        "ready_count": len(ready),
    }

    if actual > budget_tokens:
        raise BudgetExceeded("manager", actual, budget_tokens)

    return view


def _generate_redispatch_guidance(prev: PreviousAttemptSummary) -> str:
    """Generate targeted guidance based on previous attempt analysis."""
    if not prev.test_result:
        return "Previous attempt did not reach test execution. Focus on getting code to import successfully."

    if prev.test_result.import_errors:
        return f"Import errors detected in: {', '.join(prev.test_result.import_errors)}. Check __init__.py files and import paths."

    if prev.test_result.syntax_errors:
        return f"Syntax errors in: {', '.join(prev.test_result.syntax_errors)}. Fix syntax before logic."

    if prev.error_classification and prev.error_classification.category == "assertion":
        return "Tests failing on assertions. Review contract exports and ensure implementation matches specification."

    return f"Previous attempt had {prev.test_result.failure_count} test failures. Review key failures and adjust implementation."


def builder_view(
    doc: MasterDoc,
    node_id: str,
    view_spec: "ViewSpec | None" = None,
    focus_hints: "list[str] | None" = None,
    manager_note: "str | None" = None,
    exploration_hints: "dict | None" = None,
    previous_attempt: PreviousAttemptSummary | None = None,
) -> dict:
    """
    Assemble the builder subagent's view for dispatching node *node_id*.

    Contains (by default):
      - Target node: description + interface contract (what to produce)
      - Direct structural deps: full node dict including content_file path
        (caller reads the actual file; context.py has no file I/O)
      - Direct assumption deps: interface-only (no source)
      - Depth-2 ancestors: interface-only dicts
      - Ancestors beyond depth 2: one-line summaries only
      - Relevant tombstones: global always + local with keyword overlap

    Pass *view_spec* to override default builder fidelity settings.
    If None, role defaults from get_role_defaults("builder") are used.

    Pass *focus_hints* to emphasize specific nodes in the context.
    Pass *manager_note* to include free-form guidance from the manager.
    Pass *exploration_hints* to include previous session exploration data.
    Pass *previous_attempt* to include rich redispatch context from prior attempts.

    Raises KeyError if node_id is not in doc["nodes"].
    """
    from a7_rt_core.core.models import get_role_defaults

    spec = view_spec if view_spec is not None else get_role_defaults("builder")
    all_nodes = doc.get("nodes", {})
    all_deps = doc.get("dependencies", [])
    node = all_nodes[node_id]

    # Partition direct deps by type
    struct_dep_ids = set(node.get("structural_deps", []))
    assump_dep_ids = set(node.get("assumption_deps", []))

    # Ancestors: depth controlled by spec.ancestor_depth
    direct_dep_ids = struct_dep_ids | assump_dep_ids
    depth_n_ancestors = {
        nid
        for nid in ancestors(node_id, all_deps, depth=spec.ancestor_depth, nodes=all_nodes)
        if nid not in direct_dep_ids
    }
    deep_ancestors = {
        nid
        for nid in ancestors(node_id, all_deps, depth=999, nodes=all_nodes)
        if nid not in direct_dep_ids and nid not in depth_n_ancestors
    }

    retry_ctx = node.get("retry_context") or []
    retry_count = node.get("retry_count", 0)

    # Target node: fidelity controlled by spec.target_fidelity
    target: dict = {
        "node_id": node["node_id"],
        "description": node.get("description"),
        "protocol_weight": node.get("protocol_weight"),
    }
    if spec.target_fidelity in ("interface", "full"):
        target["interface"] = node.get("interface", {"exports": [], "assumptions": []})
    if spec.target_fidelity == "full":
        target["content_file"] = node.get("content_file")
    # Always include committed_files so builder can read/edit existing files on REDISPATCH
    # Filter out .test files - builders must NOT see or read test files
    if node.get("committed_files"):
        target["committed_files"] = [f for f in node["committed_files"] if not f.endswith(".test")]
    if spec.include_metadata and node.get("metadata"):
        target["metadata"] = node.get("metadata")
    if retry_ctx:
        target["retry_count"] = retry_count
        target["retry_history"] = retry_ctx

    # Structural deps: fidelity controlled by spec.struct_dep_fidelity
    def _struct_dep_view(dep_node: dict) -> dict | str | None:
        fidelity = spec.struct_dep_fidelity
        if fidelity == "none":
            return None
        if fidelity == "hash_only":
            meta = dep_node.get("metadata") or {}
            return {"content_hash": meta.get("content_hash")}
        if fidelity == "interface":
            return _interface_only(dep_node)
        # "full" - return full node but filter out test files
        dep_copy = dict(dep_node)
        if dep_copy.get("committed_files"):
            dep_copy["committed_files"] = [
                f for f in dep_copy["committed_files"] if not f.endswith(".test")
            ]
        return dep_copy

    structural_deps = {}
    for nid in struct_dep_ids:
        if nid in all_nodes:
            v = _struct_dep_view(all_nodes[nid])
            if v is not None:
                structural_deps[nid] = v

    # Assumption deps: fidelity controlled by spec.assump_dep_fidelity
    def _assump_dep_view(dep_node: dict) -> dict | None:
        fidelity = spec.assump_dep_fidelity
        if fidelity == "none":
            return None
        if fidelity == "hash_only":
            meta = dep_node.get("metadata") or {}
            return {"content_hash": meta.get("content_hash")}
        # "interface"
        return _interface_only(dep_node)

    assumption_deps = {}
    for nid in assump_dep_ids:
        if nid in all_nodes:
            v = _assump_dep_view(all_nodes[nid])
            if v is not None:
                assumption_deps[nid] = v

    # Depth-N ancestors: interface only
    ancestor_interfaces = {
        nid: _interface_only(all_nodes[nid]) for nid in depth_n_ancestors if nid in all_nodes
    }

    # Deep ancestors: fidelity controlled by spec.deep_ancestor_fidelity
    ancestor_summaries: dict = {}
    if spec.deep_ancestor_fidelity != "none":
        for nid in deep_ancestors:
            if nid not in all_nodes:
                continue
            if spec.deep_ancestor_fidelity == "summary":
                ancestor_summaries[nid] = _one_line_summary(all_nodes[nid])
            else:  # "interface"
                ancestor_summaries[nid] = _interface_only(all_nodes[nid])

    # Build structural_map from structural deps metadata (Phase 5)
    structural_map: dict = {}
    for nid in struct_dep_ids:
        dep = all_nodes.get(nid, {})
        metadata = dep.get("metadata")
        if metadata:
            # Use interface.exports if metadata preview is empty
            exports_preview = metadata.get("first_export_preview", "")
            if not exports_preview:
                interface_exports = dep.get("interface", {}).get("exports", [])
                if interface_exports:
                    exports_preview = ", ".join(interface_exports)
            structural_map[nid] = {
                "exports": exports_preview,
                "plumbing_summary": metadata.get("plumbing_summary", ""),
                "lines": metadata.get("lines", 0),
                "tokens": metadata.get("tokens", 0),
            }

    view: dict = {
        "role": "builder",
        "target": target,
        "structural_deps": structural_deps,
        "assumption_deps": assumption_deps,
        "ancestor_interfaces": ancestor_interfaces,
        "ancestor_summaries": ancestor_summaries,
    }

    # Builder must NOT be told about test file contents or even that tests exist.
    # The cardinal rule: "You MAY NOT read *.test files."
    # The harness runs tests automatically after builder submits.
    # Builder implements based on description/interface only.

    # Add file_context with structural_map if we have structural deps
    if structural_map:
        view["file_context"] = {
            "structural_map": structural_map,
            "note": "Auto-derived from dependencies. Non-binding.",
        }

    # Compute peer_patterns for package structure hints
    peer_patterns = _compute_peer_patterns(doc, node_id)
    if peer_patterns:
        view["file_context"] = view.get("file_context", {})
        view["file_context"]["peer_patterns"] = peer_patterns
        view["file_context"]["note"] = (
            view["file_context"].get("note", "")
            + " Peer patterns show existing file organization for nodes with similar prefixes."
        )

    if spec.include_tombstones:
        view["tombstones"] = _relevant_tombstones(doc, node)

    # Collect dep_tags from structural deps (experiential annotations)
    if spec.include_dep_tags:
        dep_tags = _collect_dep_tags(
            doc,
            struct_dep_ids,
            spec.min_tag_confidence,
            spec.max_dep_tags,
            propagation_depth=spec.tag_propagation_depth,
            target_node_id=node_id,
        )
        if dep_tags:
            view["dep_tags"] = dep_tags

    # Add manager guidance section when focus hints or note provided
    if focus_hints or manager_note:
        view["manager_guidance"] = {}
        if focus_hints:
            all_nodes = doc.get("nodes", {})
            view["manager_guidance"]["focus_on"] = focus_hints
            view["manager_guidance"]["previews"] = {
                nid: _one_line_summary(all_nodes[nid]) for nid in focus_hints if nid in all_nodes
            }
        if manager_note:
            view["manager_guidance"]["note"] = manager_note

    # Add exploration hints from previous sessions for warm redispatch
    if exploration_hints and exploration_hints.get("previous_attempts", 0) > 0:
        view["exploration_hints"] = {
            "previous_attempts": exploration_hints["previous_attempts"],
            "files_written": exploration_hints.get("files_written", []),
        }

    # Add rich redispatch context from previous attempt (Phase 2)
    if previous_attempt:
        view["previous_attempt"] = {
            "dispatch_number": previous_attempt.dispatch_number,
            "files_written": previous_attempt.files_written,
            "files_modified": previous_attempt.files_modified,
            "test_summary": {
                "passed": previous_attempt.test_result.passed
                if previous_attempt.test_result
                else None,
                "failure_count": previous_attempt.test_result.failure_count
                if previous_attempt.test_result
                else 0,
                "key_failures": [
                    {
                        "test_name": f.test_name,
                        "file_path": f.file_path,
                        "line": f.line_number,
                        "error": f.error_type,
                        "message": f.error_message[:200] if f.error_message else "",  # Truncated
                        "suggestion": f.suggested_fix,
                    }
                    for f in (
                        previous_attempt.test_result.key_failures
                        if previous_attempt.test_result
                        else []
                    )
                ],
                "import_errors": previous_attempt.test_result.import_errors
                if previous_attempt.test_result
                else [],
                "syntax_errors": previous_attempt.test_result.syntax_errors
                if previous_attempt.test_result
                else [],
            },
            "thoughts_from_previous": previous_attempt.thoughts[-5:]
            if previous_attempt.thoughts
            else [],  # Last 5 thoughts
            "error_classification": {
                "category": previous_attempt.error_classification.category
                if previous_attempt.error_classification
                else None,
                "severity": previous_attempt.error_classification.severity
                if previous_attempt.error_classification
                else None,
                "pattern": previous_attempt.error_classification.common_pattern
                if previous_attempt.error_classification
                else None,
            },
            "guidance": _generate_redispatch_guidance(previous_attempt),
        }

    # Add analyst_findings from node metadata (Phase 6.5: Chronicle)
    metadata = node.get("metadata") or {}
    analyst_findings = metadata.get("analyst_findings")
    if analyst_findings:
        view["analyst_findings"] = analyst_findings

    # Add test_contract from test_author (transit to builder)
    test_contract = node.get("test_contract")
    if test_contract:
        view["test_contract"] = test_contract

    # Note: exploration_hints is passed directly to builder_view, not derived from chronicle.
    # Chronicle is manager memory (dispatch, return, commit, etc.) only.
    # Agent scratchpad (tool calls, reasoning) does not cross the epistemic boundary.

    return view


def assemble_test_author_view(
    doc: MasterDoc, node_id: str, view_spec: "ViewSpec | None" = None
) -> dict:
    """
    Assemble the test-author subagent's view for node *node_id*.

    The test author writes tests against the interface contract, not the
    implementation. It receives:
      - The node's description (behavioral expectations)
      - The node's interface contract (what to verify)
      - Interface contracts of structural deps (for input type context)
      - Structure of existing grounded/provisional nodes in stage (for cohesion)

    Does NOT contain: content_file, source code, or any implementation detail.
    This separation is architecturally enforced, not disciplinary.

    Pass *view_spec* to override default test_author fidelity settings.
    If None, role defaults from get_role_defaults("test_author") are used.

    Raises KeyError if node_id is not in doc["nodes"].
    """
    from a7_rt_core.core.models import get_role_defaults

    spec = view_spec if view_spec is not None else get_role_defaults("test_author")

    all_nodes = doc.get("nodes", {})
    node = all_nodes[node_id]

    struct_dep_ids = set(node.get("structural_deps", []))
    assump_dep_ids = set(node.get("assumption_deps", []))

    retry_ctx = node.get("retry_context") or []
    retry_count = node.get("retry_count", 0)

    view: dict = {
        "role": "test_author",
        "node_id": node_id,
        "type": node.get("type", "feature"),
        "description": node.get("description"),
        "interface": node.get("interface", {"exports": [], "assumptions": []}),
    }
    if retry_ctx:
        view["retry_count"] = retry_count
        view["retry_history"] = retry_ctx
    if spec.include_metadata and node.get("metadata"):
        view["metadata"] = node.get("metadata")

    # Structural dep interfaces — fidelity to interface regardless of spec
    # (test_author never sees source; interface is the floor)
    view["structural_dep_interfaces"] = {
        nid: _interface_only(all_nodes[nid]) for nid in struct_dep_ids if nid in all_nodes
    }
    view["assumption_dep_interfaces"] = {
        nid: _interface_only(all_nodes[nid]) for nid in assump_dep_ids if nid in all_nodes
    }

    # Include structure of other grounded/provisional/suspended nodes in stage
    # This helps test author propose file structures that fit existing layout
    # Suspended nodes show attempted structure that may have hit blockages
    stage_structure: dict[str, dict] = {}
    for nid, n in all_nodes.items():
        if nid != node_id and n.get("status") in ("grounded", "provisional", "suspended"):
            stage_structure[nid] = {
                "status": n.get("status"),
                "files": n.get("committed_files", []),
                "exports": n.get("interface", {}).get("exports", []),
            }
    if stage_structure:
        view["existing_stage_structure"] = stage_structure

    if spec.include_tombstones:
        view["tombstones"] = _relevant_tombstones(doc, node, global_only=True)

    return view


def analyst_view(
    doc: MasterDoc,
    query: str,
    relevant_node_ids: list[str],
    view_spec: "ViewSpec | None" = None,
    protocol_weight: "str | None" = None,
) -> dict:
    """
    Assemble an analyst subagent's view for a specific query.

    The analyst view is query-specific: the caller selects which nodes are
    relevant. This view includes full node dicts (including content_file) and
    the dependency subgraph connecting the specified nodes.

    Used for: interface clarification, contradiction analysis, dependency audits.

    Pass *view_spec* to override default analyst fidelity settings.
    If None, role defaults from get_role_defaults("analyst") are used.

    Pass *protocol_weight* to control which protocol block is prepended to the
    system prompt (none | lean | full | a7). If None, defaults to "lean".

    Raises KeyError if any node_id in relevant_node_ids is not in doc["nodes"].
    """
    from a7_rt_core.core.models import get_role_defaults

    spec = view_spec if view_spec is not None else get_role_defaults("analyst")

    all_nodes = doc.get("nodes", {})
    node_id_set = set(relevant_node_ids)

    # Full node dicts (analyst can see source); include metadata if requested
    nodes = {}
    for nid in relevant_node_ids:
        n = all_nodes[nid]
        if spec.include_metadata:
            nodes[nid] = n  # metadata already embedded in node dict
        else:
            nodes[nid] = {k: v for k, v in n.items() if k != "metadata"}

    # Dependency subgraph: edges where at least one end is in the relevant set
    # (include both inbound and outbound to give full local context)
    subgraph_deps = [
        d
        for d in doc.get("dependencies", [])
        if d.get("from_node") in node_id_set or d.get("to_node") in node_id_set
    ]

    view: dict = {
        "role": "analyst",
        "query": query,
        "nodes": nodes,
        "dependencies": subgraph_deps,
        "protocol_weight": protocol_weight or "lean",
    }

    # All tombstones (analyst needs full graveyard context)
    if spec.include_tombstones:
        view["tombstones"] = doc.get("graveyard", [])

    return view


# ---------------------------------------------------------------------------
# View materialization (file I/O layer)
# ---------------------------------------------------------------------------


def _collect_dep_tags(
    doc: MasterDoc,
    dep_ids: set[str],
    min_confidence: int,
    max_tags: int,
    propagation_depth: int = 1,
    target_node_id: str | None = None,
) -> list[dict]:
    """
    Collect propagating file tags from dependency nodes with depth gating.

    Only includes tags where:
    - propagate=True
    - confidence >= min_confidence
    - Tag is from a node within propagation_depth hops

    Returns top N tags by confidence (N = max_tags).
    Each tag includes source_node and distance for attribution.
    """
    from a7_rt_core.core.graph import ancestors

    all_nodes = doc.get("nodes", {})
    all_deps = doc.get("dependencies", [])
    collected: list[tuple[int, dict]] = []  # (confidence, tag_dict)

    # Build a map of node_id -> distance from target
    # Start with direct deps at distance 1
    nodes_at_distance: dict[str, int] = {}
    for dep_id in dep_ids:
        nodes_at_distance[dep_id] = 1

    # If depth > 1, traverse ancestors to find transitive deps with tags
    if propagation_depth > 1 and target_node_id:
        # Get all ancestors up to propagation_depth
        for depth in range(2, propagation_depth + 1):
            # Find nodes at previous depth level
            prev_level_nodes = [nid for nid, d in nodes_at_distance.items() if d == depth - 1]
            for nid in prev_level_nodes:
                # Get direct deps of this node
                node = all_nodes.get(nid, {})
                for dep_id in node.get("structural_deps", []):
                    if dep_id not in nodes_at_distance:
                        nodes_at_distance[dep_id] = depth

    # Also include any ancestor nodes that have tags within depth
    if propagation_depth > 1 and target_node_id:
        ancestor_ids = ancestors(target_node_id, all_deps, depth=propagation_depth, nodes=all_nodes)
        for nid in ancestor_ids:
            if nid not in nodes_at_distance:
                # Calculate distance based on path length
                nodes_at_distance[nid] = propagation_depth  # Conservative

    # Collect tags from all nodes within propagation depth
    for dep_id, distance in nodes_at_distance.items():
        if distance > propagation_depth:
            continue
        dep = all_nodes.get(dep_id)
        if not dep:
            continue
        for tag in dep.get("tags") or []:
            if not tag.get("propagate", True):
                continue
            confidence = len(tag.get("confirmed_by", []))
            if confidence < min_confidence:
                continue
            collected.append(
                (
                    confidence,
                    {
                        "source_node": dep_id,
                        "category": tag.get("category", "quirk"),
                        "content": tag.get("content", ""),
                        "confidence": confidence,
                        "distance": distance,
                    },
                )
            )

    # Sort by confidence desc, take top max_tags
    collected.sort(key=lambda x: x[0], reverse=True)
    return [tag for _, tag in collected[:max_tags]]


def _materialize_node(node_view: dict, show_line_numbers: bool) -> dict:
    """
    Replace content_file path with actual content in a node view dict.
    Modifies the dict in place and returns it.
    """
    content_file = node_view.get("content_file")
    if not content_file:
        return node_view

    try:
        content = Path(content_file).read_text(encoding="utf-8")
        if show_line_numbers:
            content = _format_with_line_numbers(content)
        node_view["content"] = content
        # Remove content_file path - it's been materialized
        del node_view["content_file"]
    except (FileNotFoundError, OSError):
        # File not available - keep content_file path as indicator
        node_view["content_error"] = "file_not_found"

    return node_view


def materialize_view(view: dict, base_path: Path | None = None) -> dict:
    """
    Materialize file content into a context view.

    Reads content_file paths and injects actual file content with optional
    line number formatting based on the view's ViewSpec settings.

    Parameters
    ----------
    view : dict
        The context view from builder_view(), analyst_view(), etc.
    base_path : Path | None
        Optional base directory for resolving relative content_file paths.
        If None, paths are resolved relative to cwd.

    Returns
    -------
    dict
        A new view with content_file paths replaced by actual content.
        The original view is not modified.
    """
    import copy

    result = copy.deepcopy(view)

    # Determine if we should show line numbers based on view role
    # Default to True for builder/test_author, False for analyst/manager
    role = result.get("role", "builder")
    show_line_numbers = result.get("show_line_numbers", role in ("builder", "test_author"))

    # Materialize target node content
    target = result.get("target", {})
    if target.get("content_file"):
        if base_path and not Path(target["content_file"]).is_absolute():
            target["content_file"] = str(base_path / target["content_file"])
        _materialize_node(target, show_line_numbers)

    # Materialize structural deps with full fidelity
    struct_deps = result.get("structural_deps", {})
    for node_id, dep_view in struct_deps.items():
        if isinstance(dep_view, dict) and dep_view.get("content_file"):
            if base_path and not Path(dep_view["content_file"]).is_absolute():
                dep_view["content_file"] = str(base_path / dep_view["content_file"])
            _materialize_node(dep_view, show_line_numbers)

    # Analyst view has nodes dict instead of target/structural_deps
    nodes = result.get("nodes", {})
    for node_id, node_view in nodes.items():
        if isinstance(node_view, dict) and node_view.get("content_file"):
            if base_path and not Path(node_view["content_file"]).is_absolute():
                node_view["content_file"] = str(base_path / node_view["content_file"])
            _materialize_node(node_view, show_line_numbers)

    return result
