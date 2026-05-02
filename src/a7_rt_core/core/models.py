"""
A7-RT Schema-as-Code
Pydantic models for the A7-RT cognitive runtime.

Enforces A7 invariants mechanically:
  - Node status transitions (near→provisional→grounded; poison from anywhere)
  - Glue nodes coordinate N dependencies (no cardinality constraint)
  - Protocol weight 'none' has a status ceiling of provisional
  - Suspended nodes must carry a SuspensionReason
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NodeStatus(str, Enum):
    NEAR = "near"
    PROVISIONAL = "provisional"
    GROUNDED = "grounded"
    POISONED = "poisoned"
    SUSPENDED = "suspended"


class NodeType(str, Enum):
    FEATURE = "feature"
    GLUE = "glue"
    TEST = "test"


class ProtocolWeight(str, Enum):
    NONE = "none"
    LEAN = "lean"
    FULL = "full"


class SuspensionType(str, Enum):
    NEAR = "near"  # one step away; retry with more context
    FAR = "far"  # external data required; await human
    VOID = "void"  # epistemic blind spot; stop trying
    WILD = "wild"  # generative timeout; quarantined, don't retry in same scope


class DependencyType(str, Enum):
    STRUCTURAL = "structural"  # module-level import; mechanically verifiable
    ASSUMPTION = "assumption"  # behavioural property; requires human judgment


class StageStatus(str, Enum):
    ACTIVE = "active"
    SEALED = "sealed"


class ManagerMode(str, Enum):
    AUTONOMOUS = "autonomous"  # normal operation
    DRAIN = "drain"  # turn >= drain_turn; no new dispatches
    DEAD = "dead"  # turn >= max_turns; lifecycle complete


# ---------------------------------------------------------------------------
# Transition table
# Poison (POISONED) is always a valid destination — downstream of [☠] is [☠].
# Loaded from external JSON for easy editing; see schemas/harness/state_transitions.json
# ---------------------------------------------------------------------------


def _load_transitions() -> dict[NodeStatus, frozenset[NodeStatus]]:
    """Load transition table from schema file."""
    try:
        from a7_rt_core.schemas import load_state_transitions

        data = load_state_transitions()
        transitions = data.get("transitions", {})

        return {
            NodeStatus.NEAR: frozenset(
                NodeStatus(s) for s in transitions.get("near", {}).get("valid_next", [])
            ),
            NodeStatus.PROVISIONAL: frozenset(
                NodeStatus(s) for s in transitions.get("provisional", {}).get("valid_next", [])
            ),
            NodeStatus.GROUNDED: frozenset(
                NodeStatus(s) for s in transitions.get("grounded", {}).get("valid_next", [])
            ),
            NodeStatus.POISONED: frozenset(
                NodeStatus(s) for s in transitions.get("poisoned", {}).get("valid_next", [])
            ),
            NodeStatus.SUSPENDED: frozenset(
                NodeStatus(s) for s in transitions.get("suspended", {}).get("valid_next", [])
            ),
        }
    except Exception:
        # Fallback to hardcoded transitions if schema loading fails
        return {
            NodeStatus.NEAR: frozenset(
                {
                    NodeStatus.PROVISIONAL,
                    NodeStatus.GROUNDED,
                    NodeStatus.POISONED,
                    NodeStatus.SUSPENDED,
                }
            ),
            NodeStatus.PROVISIONAL: frozenset(
                {
                    NodeStatus.GROUNDED,
                    NodeStatus.POISONED,
                    NodeStatus.SUSPENDED,
                }
            ),
            NodeStatus.GROUNDED: frozenset({NodeStatus.POISONED}),
            NodeStatus.POISONED: frozenset(),
            NodeStatus.SUSPENDED: frozenset(
                {
                    NodeStatus.NEAR,
                    NodeStatus.GROUNDED,
                }
            ),
        }


_VALID_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = _load_transitions()


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class InterfaceContract(BaseModel):
    """What a node exports and what it assumes about its environment."""

    exports: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    raises: list[str] | None = None
    guarantees: list[str] | None = None


class SuspensionReason(BaseModel):
    """Carried by nodes in SUSPENDED status."""

    type: SuspensionType
    detail: str


class Dependency(BaseModel):
    """
    A directed edge in the project graph.
    'from_node' depends on 'to_node'.
    """

    from_node: str
    to_node: str
    type: DependencyType
    verified: bool = True
    discovered_by: Optional[Literal["human", "subagent"]] = None
    turn: Optional[int] = None


# ---------------------------------------------------------------------------
# ViewSpec
# ---------------------------------------------------------------------------


class ViewSpec(BaseModel):
    """
    Declarative fidelity preferences for context assembly.

    Stored in node.view_spec or passed as a per-dispatch override on
    DispatchAction. If absent, context.py uses role defaults via
    get_role_defaults(role).

    Fields
    ------
    target_fidelity
        How much of the *target* node to include.
        "minimal"   — node_id + description only
        "interface" — minimal + interface contract
        "full"      — interface + content_file (caller reads file)

    struct_dep_fidelity
        Fidelity for structural dependency nodes.
        "none"      — omit entirely
        "hash_only" — content_hash only (staleness detection)
        "interface" — interface contract only (no source)
        "full"      — full node dict including content_file

    assump_dep_fidelity
        Fidelity for assumption dependency nodes.
        "none" | "hash_only" | "interface"

    ancestor_depth
        How many hops of ancestors to traverse (0 = none, 2 = default).

    deep_ancestor_fidelity
        Fidelity for ancestors beyond ancestor_depth.
        "none"      — omit
        "summary"   — one-line summary string
        "interface" — interface contract

    include_tombstones
        Whether to include relevant tombstones in the view.

    include_metadata
        Whether to include NodeMetadata fields in the view.

    include_dep_tags
        Whether to include file tags from structural deps.

    min_tag_confidence
        Only tags with confidence >= this value are included.

    max_dep_tags
        Maximum number of dep tags per view (budget protection).

    tag_propagation_depth
        How many hops to propagate file tags from dependencies.
        1 = direct deps only (default), 2 = deps of deps, etc.
        Tags include distance annotation for transparency.

    show_line_numbers
        Whether to prepend line numbers to file content in the view.
        Default True for builder/test_author; False for analyst/manager.
    """

    model_config = {"frozen": True}

    target_fidelity: Literal["minimal", "interface", "full"] = "full"
    struct_dep_fidelity: Literal["none", "hash_only", "interface", "full"] = "interface"
    assump_dep_fidelity: Literal["none", "hash_only", "interface"] = "interface"
    ancestor_depth: int = 2
    deep_ancestor_fidelity: Literal["none", "summary", "interface"] = "summary"
    include_tombstones: bool = True
    include_metadata: bool = False
    include_dep_tags: bool = True
    min_tag_confidence: int = 1
    max_dep_tags: int = 3
    tag_propagation_depth: int = 1
    show_line_numbers: bool = True


# Role defaults — canonical fidelity presets per subagent role.
# context.py calls get_role_defaults(role) when no ViewSpec is supplied.

_ROLE_DEFAULTS: dict[str, ViewSpec] = {
    "builder": ViewSpec(
        target_fidelity="full",
        struct_dep_fidelity="full",
        assump_dep_fidelity="interface",
        ancestor_depth=2,
        deep_ancestor_fidelity="summary",
        include_tombstones=True,
        include_metadata=False,
        show_line_numbers=True,
    ),
    "test_author": ViewSpec(
        target_fidelity="interface",
        struct_dep_fidelity="interface",
        assump_dep_fidelity="interface",
        ancestor_depth=1,
        deep_ancestor_fidelity="none",
        include_tombstones=False,
        include_metadata=False,
        show_line_numbers=True,
    ),
    "analyst": ViewSpec(
        target_fidelity="full",
        struct_dep_fidelity="full",
        assump_dep_fidelity="interface",
        ancestor_depth=2,
        deep_ancestor_fidelity="summary",
        include_tombstones=True,
        include_metadata=True,
        show_line_numbers=False,
    ),
    "manager": ViewSpec(
        target_fidelity="interface",
        struct_dep_fidelity="none",
        assump_dep_fidelity="none",
        ancestor_depth=0,
        deep_ancestor_fidelity="none",
        include_tombstones=False,
        include_metadata=True,
        show_line_numbers=False,
    ),
}


def get_role_defaults(role: str) -> ViewSpec:
    """
    Return the canonical ViewSpec for *role*.

    Falls back to the builder preset for unknown roles so callers never
    receive None — the invariant is that every dispatch has a ViewSpec.
    """
    return _ROLE_DEFAULTS.get(role, _ROLE_DEFAULTS["builder"])


# ---------------------------------------------------------------------------
# ChronicleEntry
# ---------------------------------------------------------------------------


class ChronicleEntry(BaseModel):
    """
    Single entry in a node's chronicle — an ordered trace of actions by all actors.

    The chronicle is process memory: manager thoughts, agent tool calls,
    analyst findings. Manager sees full chronicle when node is active;
    agent sees only its own exploration via exploration_hints derivation.
    """

    model_config = {"frozen": True}

    turn: int  # Global turn number
    actor: Literal["manager", "agent", "analyst"]  # Who acted
    action: str  # What happened
    metadata: dict = Field(default_factory=dict)  # Action-specific data
    manager_thought: Optional[str] = None  # Manager reasoning (manager only)
    manager_intent: Optional[str] = None  # Manager goal (manager only)
    dispatch_seq: Optional[int] = None  # Which dispatch attempt (agent entries)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# FileTag
# ---------------------------------------------------------------------------


class FileTag(BaseModel):
    """
    Experiential annotation on a file node. Agent-authored, confidence-accumulated.

    Captures "gotchas" discovered during implementation:
    - verify_token raises ValueError not InvalidTokenError on malformed headers
    - configure_backend() must be called before get/set or returns silent None
    - JWT_SECRET must be set at process start, not lazy-loaded

    Storage: node.tags list in master.json. Survives stage sealing.
    """

    model_config = {"frozen": True}

    tag_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    node_id: str  # The node this tag annotates (usually a dependency)
    author_role: str  # "builder" | "test_author" | "analyst" | "human"
    author_turn: int = Field(default=0)
    category: Literal["quirk", "order", "scope", "perf", "warning"]
    content: str = Field(max_length=200)  # The note, max 200 chars
    propagate: bool = True  # Appear in views of dependent nodes?
    confirmed_by: list[str] = Field(default_factory=list)  # Roles that verified

    @property
    def confidence(self) -> int:
        """Confidence is the number of independent confirmations."""
        return len(self.confirmed_by)


# ---------------------------------------------------------------------------
# NodeMetadata
# ---------------------------------------------------------------------------


class NodeMetadata(BaseModel):
    """
    Mechanical, harness-computed measurements for a node's content.

    Never LLM-generated. Computed at commit time in _apply_return() and at
    import time in execute_plan(). Stored in node.metadata.

    Fields:
      tokens              — word count of content (len(content.split()))
      lines               — line count (content.count('\\n') + 1)
      content_hash        — SHA-256 hex digest, first 16 chars (staleness detection)
      first_export_preview— first exported function/class signature (single line)
      plumbing_summary    — 1-2 line structural summary (AST or regex extracted)
      computed_at         — UTC timestamp of last computation
      iteration_history   — Last N dispatch iteration counts (telemetry)
      avg_iterations      — Historical average for manager calibration
      chronicle           — Ordered process trace (actor, action, metadata)
      analyst_findings    — Curated knowledge from analyst examinations (list)
    """

    model_config = {"frozen": True, "extra": "allow"}

    tokens: int = 0
    lines: int = 0
    content_hash: str = ""
    first_export_preview: str = ""
    plumbing_summary: str = ""
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    iteration_history: list[int] = Field(default_factory=list)
    avg_iterations: float = 0.0
    chronicle: list[ChronicleEntry] = Field(default_factory=list)
    analyst_findings: list[dict] = Field(default_factory=list)

    @field_validator("analyst_findings", mode="before")
    @classmethod
    def _coerce_legacy_findings(cls, v: Any) -> list[dict]:
        """Coerce legacy dict or None to list[dict]."""
        if v is None:
            return []
        if isinstance(v, dict):
            # Legacy single finding as dict - wrap in list
            return [v]
        if isinstance(v, list):
            return v
        return []


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """
    The core unit of work in the A7-RT graph.

    Invariants enforced at construction:
      1. Glue nodes coordinate N structural deps (cardinality unconstrained — signal
         is the composition function in exports, not dep count).
      2. Protocol weight 'none' → status ceiling is provisional (not grounded).
      3. Status 'suspended' requires a suspension_reason.
    """

    model_config = {"frozen": True}

    node_id: str
    stage_id: str
    type: NodeType
    status: NodeStatus = NodeStatus.NEAR
    description: str
    protocol_weight: ProtocolWeight = ProtocolWeight.LEAN
    interface: InterfaceContract = Field(default_factory=InterfaceContract)
    structural_deps: list[str] = Field(default_factory=list)
    assumption_deps: list[str] = Field(default_factory=list)
    suspension_reason: Optional[SuspensionReason] = None
    content_file: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retry_count: int = 0
    max_retries: int = 3
    retry_context: list[dict] = Field(default_factory=list)
    metadata: Optional[NodeMetadata] = None
    metadata_map: dict[str, Any] = Field(default_factory=dict)
    view_spec: Optional[ViewSpec] = None
    tags: list[FileTag] = Field(default_factory=list)
    pr_note: Optional[str] = None  # Agent-to-manager signal, soft limit 500, hard limit 1000 chars
    test_contract: Optional[str] = (
        None  # Test author to builder: behavioral contract, edge cases, priorities
    )
    committed_files: list[str] = Field(
        default_factory=list
    )  # Files written by agent in last commit

    # -- Validators --

    @model_validator(mode="after")
    def _weight_none_ceiling(self) -> Node:
        if self.protocol_weight == ProtocolWeight.NONE and self.status == NodeStatus.GROUNDED:
            raise ValueError(
                f"Node '{self.node_id}' has protocol_weight=none; "
                "status ceiling is provisional — cannot be grounded."
            )
        return self

    @model_validator(mode="after")
    def _suspended_requires_reason(self) -> Node:
        if self.status == NodeStatus.SUSPENDED and self.suspension_reason is None:
            raise ValueError(
                f"Node '{self.node_id}' is suspended but has no suspension_reason. "
                "Provide SuspensionReason(type=..., detail=...)."
            )
        return self

    # -- Transition --

    def transition(self, new_status: NodeStatus, **updates) -> Node:
        """
        Return a new Node with new_status applied.
        Raises ValueError if the transition is not permitted.

        Pass additional keyword args to update other fields simultaneously
        (e.g. suspension_reason when transitioning to SUSPENDED).
        """
        allowed = _VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid status transition for node '{self.node_id}': "
                f"{self.status.value} → {new_status.value}. "
                f"Allowed: {[s.value for s in allowed] or 'none (terminal)'}"
            )
        data = self.model_dump()
        data["status"] = new_status
        data.update(updates)
        return Node.model_validate(data)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class Stage(BaseModel):
    """
    A bounded scope within a project.

    Active: full node graph visible to manager.
    Sealed: compressed to summary + exported interfaces; excluded from context views.
    """

    model_config = {"frozen": True}

    stage_id: str
    project_id: str
    name: str
    status: StageStatus = StageStatus.ACTIVE
    node_ids: list[str] = Field(default_factory=list)
    sealed_at: Optional[datetime] = None
    summary: Optional[str] = None
    exported_interfaces: dict[str, InterfaceContract] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sealed_stage_consistency(self) -> Stage:
        if self.status == StageStatus.SEALED and self.sealed_at is None:
            raise ValueError(f"Stage '{self.stage_id}' is sealed but has no sealed_at timestamp.")
        return self


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """
    Immutable project configuration.
    Seeds the master document; never mutated after creation.
    """

    model_config = {"frozen": True}

    project_id: str
    name: str
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stage_ids: list[str] = Field(default_factory=list)
    manager_max_turns: int = 25
    drain_turn: int = 20
    provisional_depth_limit: int = 2

    @model_validator(mode="after")
    def _drain_before_max(self) -> Project:
        if self.drain_turn >= self.manager_max_turns:
            raise ValueError(
                f"drain_turn ({self.drain_turn}) must be less than manager_max_turns ({self.manager_max_turns})."
            )
        return self


# ---------------------------------------------------------------------------
# Planning State (Manager working memory)
# ---------------------------------------------------------------------------


class ChecklistItem(BaseModel):
    """
    A pre-SEAL verification concern tracked by the manager.
    Ephemeral working memory — discarded on SEAL.
    """

    model_config = {"frozen": True}

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str
    status: Literal["pending", "in_progress", "satisfied", "void"] = "pending"


class PlanningState(BaseModel):
    """
    Structured working memory for manager intent and verification concerns.
    Replaces the free-form stage_intent with actionable decomposition.
    Narrative survives SEAL (appended to stage.summary); checklist is ephemeral.
    """

    model_config = {"frozen": True}

    narrative: Optional[str] = None
    checklist: list[ChecklistItem] = Field(default_factory=list)
    updated_at: int = 0


# ---------------------------------------------------------------------------
# Manager State (Ephemeral, persisted for resume)
# ---------------------------------------------------------------------------


class ManagerState(BaseModel):
    """
    Ephemeral state for one manager lifecycle (born at stage start, dies at turn 25).
    Persisted to master at checkpoint and on death so the next manager can resume.
    """

    model_config = {"frozen": True}

    current_stage_id: str
    turn: int = 0
    manager_max_turns: Optional[int] = None  # None = unlimited (default for TUI)
    drain_turn: Optional[int] = 100  # Soft pressure toward SEAL
    mode: ManagerMode = ManagerMode.AUTONOMOUS
    in_flight: list[str] = Field(default_factory=list)  # node_ids dispatched, not yet returned
    human_input_queue: list[dict] = Field(default_factory=list)
    planning: Optional[PlanningState] = None  # Replaces stage_intent
    checkpoint_at: Optional[datetime] = None
    wild_counts: dict[str, int] = Field(default_factory=dict)
    pending_commits: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _turn_within_bounds(self) -> ManagerState:
        if self.manager_max_turns is not None and self.turn > self.manager_max_turns:
            raise ValueError(
                f"turn ({self.turn}) exceeds manager_max_turns ({self.manager_max_turns}). "
                "The manager should be dead."
            )
        return self

    @model_validator(mode="after")
    def _mode_consistency(self) -> ManagerState:
        if (
            self.manager_max_turns is not None
            and self.turn >= self.manager_max_turns
            and self.mode != ManagerMode.DEAD
        ):
            raise ValueError(
                f"turn={self.turn} >= manager_max_turns={self.manager_max_turns} but mode is '{self.mode.value}'. "
                "Expected mode=dead."
            )
        if (
            self.drain_turn is not None
            and self.turn >= self.drain_turn
            and self.mode == ManagerMode.AUTONOMOUS
        ):
            raise ValueError(
                f"turn={self.turn} >= drain_turn={self.drain_turn} but mode is 'autonomous'. "
                "Expected mode=drain or dead."
            )
        return self

    @property
    def is_drain(self) -> bool:
        return self.mode == ManagerMode.DRAIN

    @property
    def is_dead(self) -> bool:
        return self.mode == ManagerMode.DEAD


# ---------------------------------------------------------------------------
# Manager action types
#
# The manager LLM emits exactly one action per turn. The harness routes it.
# Defined here so both harness.py and any future manager driver can import
# without circular deps.
# ---------------------------------------------------------------------------


class ManagerAction:
    """Base class for all manager actions. Harness dispatches on type."""

    def __init__(self) -> None:
        self.intent: str | None = None


class DispatchAction(ManagerAction):
    """Send work to a stateless subagent."""

    def __init__(
        self,
        node_id: str,
        role: str,
        weight: str = "lean",
        view_spec: "Optional[ViewSpec]" = None,
        focus_hints: "Optional[list[str]]" = None,
        expected_iterations: int = 5,
        manager_note: "Optional[str]" = None,
        # Analyst-specific parameters (required when role="analyst")
        scope: "Optional[str]" = None,  # "node" | "project" | "external"
        query: "Optional[str]" = None,  # The specific question to answer
        target_nodes: "Optional[list[str]]" = None,  # Nodes to examine (project scope)
        clear_findings: "Optional[list[str]]" = None,  # Node IDs to clear analyst findings for
    ) -> None:
        self.node_id = node_id
        self.role = role  # "builder" | "test_author" | "analyst"
        self.weight = weight  # "none" | "lean" | "full"
        self.view_spec = view_spec  # per-dispatch fidelity override; None = role defaults
        self.focus_hints = focus_hints  # Node IDs to emphasize in context
        self.expected_iterations = expected_iterations  # Soft guidance on difficulty
        self.manager_note = manager_note  # Free-form rationale for dispatch
        # Analyst scope routing
        self.scope = scope  # "node" | "project" | "external"
        self.query = query  # The specific question to answer
        self.target_nodes = target_nodes or []  # For project/external scope
        self.clear_findings = clear_findings or []  # Clear findings for resolved nodes


class ValidateAction(ManagerAction):
    """
    Check schema of a pending subagent return sitting in pending_returns.
    The harness runs schema validation; result appears on the next board.
    """

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


class CommitAction(ManagerAction):
    """
    Accept a validated result and declare its final status.
    Harness runs hard-path test (if grounded), propagates consequences, git-commits.
    """

    def __init__(
        self,
        node_id: str,
        status: "NodeStatus",
        reason: str = "",
        manager_note: "str | None" = None,
        clear_findings: "Optional[list[str]]" = None,
    ) -> None:
        self.node_id = node_id
        self.status = status
        self.reason = reason
        self.manager_note = manager_note
        self.clear_findings = clear_findings or []


class ConsultAction(ManagerAction):
    """Ask the A7 reasoning engine a question. Result lands in pending_consult next turn."""

    def __init__(
        self,
        question: str,
        relevant_node_ids: "list[str]",
        constraints: "Optional[list[str]]" = None,
    ) -> None:
        self.question = question
        self.relevant_node_ids = relevant_node_ids
        self.constraints = constraints or []


class SuspendAction(ManagerAction):
    """Mark a node blocked without dispatching it."""

    def __init__(self, node_id: str, type: "SuspensionType", detail: str) -> None:
        self.node_id = node_id
        self.type = type
        self.detail = detail


class SealAction(ManagerAction):
    """Close the current stage. Harness validates all nodes resolved before executing."""

    def __init__(self, summary: str) -> None:
        self.summary = summary


class HaltAction(ManagerAction):
    """Stop and checkpoint. Wait for human intervention."""

    def __init__(self, reason: str, invariant: "Optional[str]" = None) -> None:
        self.reason = reason
        self.invariant = invariant


class RedispatchAction(ManagerAction):
    """
    Re-send a failed node to a subagent with retry context injected.

    Distinct from DispatchAction so the harness can:
      - Check node.retry_count against node.max_retries (HALT if exhausted)
      - Increment retry_count on the node
      - Inject retry_context into the subagent view
      - Log a 'redispatch' event (separate from 'dispatch' in the audit log)

    manager_note is required: it explains what failed and what the next
    subagent should do differently. It is appended to retry_context before
    the redispatch so the subagent sees it.
    """

    def __init__(
        self,
        node_id: str,
        role: str,
        weight: str = "lean",
        manager_note: str = "",
        view_spec: "Optional[ViewSpec]" = None,
        clear_findings: "Optional[list[str]]" = None,
    ) -> None:
        self.node_id = node_id
        self.role = role
        self.weight = weight
        self.manager_note = manager_note
        self.view_spec = view_spec  # per-dispatch fidelity override; None = role defaults
        self.clear_findings = clear_findings or []


class UpdatePlanAction(ManagerAction):
    """
    Update manager planning state: narrative intent and pre-SEAL checklist.
    Full replacement semantics — manager emits complete new state.
    """

    def __init__(
        self,
        narrative: "Optional[str]" = None,
        checklist: "Optional[list[ChecklistItem]]" = None,
    ) -> None:
        self.narrative = narrative
        self.checklist = checklist or []


# ---------------------------------------------------------------------------
# Subagent return types
#
# Defined here (not in harness.py) so that subagent.py can import them
# without creating a circular dependency: harness ← subagent ← harness.
# ---------------------------------------------------------------------------


class SubagentError(Exception):
    """
    Raised by the subagent hook when the LLM call fails unrecoverably.
    The harness catches this and marks the node suspended:wild (persistent
    API failure) or halts (context-length overflow).
    """


class ExplorationEntry(BaseModel):
    """
    Single tool call within an agent session.
    Tracks what was explored during a dispatch for warm redispatch.
    """

    model_config = {"frozen": True}

    tool: str  # "read_file", "grep_content", "list_files", "record_thought"
    args: dict  # {"path": "auth/jwt.py"} or {"pattern": "def"} or {"thought": "..."}
    turn: int  # Turn when executed
    timestamp: str  # ISO8601
    intent: str | None = None  # Optional: why this tool was called


class ThoughtEntry(BaseModel):
    """
    Structured reasoning captured during agent exploration.
    Persists across dispatches to guide future attempts and avoid dead ends.
    """

    model_config = {"frozen": True}

    thought: str  # Max 500 chars
    category: Literal["hypothesis", "contradiction", "plan", "question"]
    relates_to: str | None = None  # File path or node_id
    turn: int  # Turn when recorded
    timestamp: str  # ISO8601


class AgentSession(BaseModel):
    """
    Complete record of one dispatch.
    Enables warm redispatch by tracking what previous attempts produced.
    """

    model_config = {"frozen": True}

    session_id: str  # UUID v4
    node_id: str  # Target node
    dispatch_turn: int  # Turn when dispatched
    role: str  # "builder", "test_author", "analyst"
    attempt_number: int  # 1st, 2nd, 3rd dispatch of this node
    status: Literal["running", "completed", "suspended", "failed", "interrupted"]
    iterations_used: int = 0
    files_written: list[str] = Field(default_factory=list)  # Grounded outputs only
    created_at: str  # ISO8601
    ended_at: str | None = None
    pr_note: Optional[str] = None  # Agent-to-manager signal, soft limit 500, hard 1000 chars


class IterationStatus(Enum):
    """
    Status of agent iteration for fresh-context redispatch architecture.

    PROGRESS     : Files written/changed, moving forward
    STALLED      : No changes, retrying same operations
    TEST_FAILING : Tests run but fail
    TEST_PASSING : Tests pass, ready to submit
    """

    PROGRESS = "progress"
    STALLED = "stalled"
    TEST_FAILING = "test_failing"
    TEST_PASSING = "test_passing"


class SubagentReturn:
    """
    Parsed, normalised return from any subagent dispatch.
    Produced by subagent.py; consumed by harness.py.

    status           : the new NodeStatus the subagent asserts
    content          : (DEPRECATED) Single file content string. Use files dict instead.
    files            : Multi-file work product as {path: content}. Atomic write semantics.
    edits            : Line-addressed edit operations to apply to existing files.
    interface_update : fields to merge into the node's InterfaceContract
    new_deps         : undeclared deps discovered during work (harness adds as unverified)
    suspension_reason: populated when status == SUSPENDED
    role             : the subagent role that produced this return
                       ("builder" | "test_author" | "analyst"); used by harness
                       to route content to the correct file and gate hard-path.
    escalate         : True if the subagent requests re-dispatch at higher protocol weight.
    file_tags        : Experiential annotations on dependencies discovered during work.
    iterations_used  : Actual loops consumed in agent loop (telemetry)
    tool_usage       : Tool call counts for efficiency analysis (telemetry)
    test_runs        : Test execution results for auto-commit validation (Phase 4)
    analysis_result  : Analyst subagent structured findings (scope, findings, confidence)
    checkpoint_reached: True if agent paused at checkpoint (not done)
    iteration_status : Why the agent is returning (progress, stalled, etc.)
    thoughts_recorded: Persisted reasoning for warm redispatch
    """

    def __init__(
        self,
        status: "NodeStatus",
        content: "Optional[str]" = None,
        files: "Optional[dict[str, str]]" = None,
        edits: "Optional[list[dict]]" = None,
        interface_update: "Optional[dict]" = None,
        new_deps: "Optional[list[dict]]" = None,
        suspension_reason: "Optional[SuspensionReason]" = None,
        role: str = "builder",
        escalate: bool = False,
        file_tags: "Optional[list[dict]]" = None,
        iterations_used: int = 1,
        tool_usage: "Optional[dict[str, int]]" = None,
        session_id: str | None = None,
        pr_note: str | None = None,
        test_contract: str | None = None,
        test_runs: "Optional[list[dict]]" = None,
        analysis_result: "Optional[dict]" = None,
        checkpoint_reached: bool = False,
        iteration_status: "Optional[IterationStatus]" = None,
        thoughts_recorded: "Optional[list[dict]]" = None,
    ) -> None:
        self.status = status
        self.content = content
        self.files = files or {}
        self.edits = edits or []
        self.interface_update = interface_update or {}
        self.new_deps = new_deps or []
        self.suspension_reason = suspension_reason
        self.role = role
        self.escalate = escalate
        self.file_tags = file_tags or []
        self.iterations_used = iterations_used
        self.tool_usage = tool_usage or {}
        self.session_id = session_id
        self.pr_note = pr_note[:1000] if pr_note else None
        self.test_contract = test_contract[:1000] if test_contract else None
        self.test_runs = test_runs or []
        self.analysis_result = analysis_result or {}
        self.checkpoint_reached = checkpoint_reached
        self.iteration_status = iteration_status
        self.thoughts_recorded = thoughts_recorded or []


# ---------------------------------------------------------------------------
# Factory methods
# ---------------------------------------------------------------------------


def create_project(
    name: str,
    description: str,
    *,
    manager_max_turns: int = 25,
    drain_turn: int = 20,
    provisional_depth_limit: int = 2,
) -> Project:
    """Create a new project with a generated UUID."""
    return Project(
        project_id=str(uuid.uuid4()),
        name=name,
        description=description,
        manager_max_turns=manager_max_turns,
        drain_turn=drain_turn,
        provisional_depth_limit=provisional_depth_limit,
    )


def create_stage(project_id: str, name: str) -> Stage:
    """Create a new active stage attached to a project."""
    return Stage(
        stage_id=str(uuid.uuid4()),
        project_id=project_id,
        name=name,
    )


def dispatch_node(
    node_id: str,
    stage_id: str,
    type: NodeType,
    description: str,
    *,
    protocol_weight: ProtocolWeight = ProtocolWeight.LEAN,
    structural_deps: list[str] | None = None,
    assumption_deps: list[str] | None = None,
    interface: InterfaceContract | None = None,
) -> Node:
    """
    Create a Node ready for its first dispatch (status=near).

    Glue nodes coordinate N structural deps — no cardinality constraint.
    The semantic signal for glue is a single composition function in exports,
    not the number of dependencies.
    """
    return Node(
        node_id=node_id,
        stage_id=stage_id,
        type=type,
        description=description,
        protocol_weight=protocol_weight,
        structural_deps=structural_deps or [],
        assumption_deps=assumption_deps or [],
        interface=interface or InterfaceContract(),
        status=NodeStatus.NEAR,
    )
