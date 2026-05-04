# A7-RT 0.1.0b1
[CoreLathe Website and Contact](https://corelathe.com)

[CLI Usage](https://github.com/CoreLathe/A7-RT/blob/main/CLI_USAGE.md)

[Contracts](https://github.com/CoreLathe/A7-RT/blob/main/CONTRACTS.md)

AI coding assistants write code quickly, but they break production systems just as fast. A7-RT is a transactional build system that treats LLM agents as unreliable compilers. It verifies every artifact before it touches your repository.

## What It Is

A7-RT is an amnesiac orchestration runtime for AI coding agents. The manager does not accumulate chat history between turns; the ledger remembers everything. Agents execute as ephemeral workers within a transactional build graph, where each decision is a stateless function of the current board state.  No context windows, no drift, no rotting conversation history. It occupies the gap between exploratory AI (where ambiguity is productive) and production software (where ambiguity is dangerous), ensuring only verified code enters version control.

Workflows are governed by executable contracts (interface signatures, assumptions, guarantees) rather than conversational context or prompt engineering. The human provides intent and the system enforces that implementation matches the contract through adversarial verification.

A7-RT orchestrates AI agents through a dependency graph with the same rigor as a modern build system. Nodes represent capability boundaries with explicit interface contracts. Agents execute in isolated environments. Code only enters version control after passing compilation and test verification.

**Core Mechanisms:**

- **ShadowFS:** Every agent runs in a hermetic filesystem sandbox. Failed attempts stay in shadow; only verified code reaches your working tree.
- **Adversarial Verification:** Test authors write contracts and tests. Builders implement against those contracts without reading test files. The harness validates through hard-path execution before any commit.
- **Ephemeral Context:** The manager LLM receives a computed BoardView and emits exactly one action. Context is discarded after each turn. The system never accumulates conversational drift or rotting context windows discarded immediately after use.
- **Append-Only Ledger:** Project state lives in `master.json`. Interrupt a session today, resume in six months, pay zero catch-up cost.

**The Workflow:**

1. **Contract:** Define the node graph with interface signatures, dependencies, and behavioral guarantees.
2. **Dispatch:** Harness computes the ready set, spawns an agent in ShadowFS with a fresh context view.
3. **Verify:** Schema-check returns and run hard-path validation (compilation, tests).
4. **Commit:** Grounded nodes write to the content directory and commit to git. Provisional nodes await validation.
5. **Seal:** Stage completes when all nodes are grounded or appropriately suspended.

Unlike conversational AI coding tools that accumulate state and hallucinate across turns, A7-RT composes deterministic build steps. Each agent invocation is independent. Dependencies are explicit. Failures are contained.

## Contracts

Contracts sit on a spectrum between exploratory intent and mechanical verification. They are structured prompts with typed holes that the system grounds through adversarial testing.

**Mode A (Contract-First):** Exports are pre-declared. Test authors write tests against that surface, then encode a subcontract in metadata that tells builders the contract without revealing test implementation. Builders implement to the locked interface.

```json
{
  "id": "state.store",
  "interface": {
    "exports": [
      "def create_session(store: StateStore, ttl_seconds: int) -> Session",
      "def get_session(store: StateStore, session_id: str) -> Session|None"
    ],
    "guarantees": ["Expired sessions return None", "Thread-safe operations"]
  }
}
```

**Mode B (Test-First):** Exports start empty. You describe behavioral intent. Test authors define the contract through `test_contract`. The API surface emerges from implementation rather than being dictated. Use when you know the behavior but not the shape.

```json
{
  "id": "matcher.engine",
  "interface": {
    "exports": [],
    "guarantees": ["First matching rule wins", "O(m * n) match time"]
  }
}
```

**Dynamism:** A node is a capability boundary, not a file. It scales from single pure functions to service interfaces. Each node carries its own retry budget and suspension status. The system tracks failure causality through tombstone records and poisons downstream dependents transitively.

A7-RT contracts are not lightweight and can be as cognitively demanding as writing code. The payoff isn't reduced thinking, it's architectural clarity: contracts make dependencies and assumptions explicit, enabling parallel implementation and precise failure attribution. The harness doesn't make building software easier; it makes building software more systematic.


---

## Quick Start

```bash
# Initialize a new session
a7-rt init /path/to/session

# Run a session headlessly
a7-rt run /path/to/session --stage-id=stage-1

# View session narrative
a7-rt narrative /path/to/session --node=auth.jwt
```

> **Note:** All commands work identically via `python -m a7_rt_core.cli` (e.g., `python -m a7_rt_core.cli init /path/to/session`). Use whichever is more convenient for your workflow.

---

## System Invariants

The harness enforces these invariants regardless of manager action. Violations raise `HaltSignal` and checkpoint session state for human review.

**Evidence Over Assertion**

Nodes begin as `near` (claimed by agent), advance to `provisional` (schema-validated), and only reach `grounded` after hard-path tests pass: compilation, execution, or formal proof. Status is not self-declared; it is earned through verification.

**Failure Propagation**

When a node is marked `poisoned`, all downstream dependents are transitively poisoned through structural and assumption dependencies. The system tracks causal chains via tombstone records. A poisoned node cannot be committed or validated until the underlying failure is resolved and the node is redispatched.

**Typed Suspension**

When a node cannot proceed, it is suspended with a specific reason:
- `near` — One verification step away from resolution
- `far` — External data required; await human  
- `void` — Epistemically blocked (contradiction or unanswerable)
- `wild` — Schema violation or generative timeout

Suspended nodes are quarantined, not forgotten. Same-node `wild` recurrence triggers halt.

**Stateless Manager, Stateful Harness**

The manager LLM receives a read-only board view, emits one action, and terminates. It does not retain memory between turns. The harness maintains master document state, validates actions against invariants, coordinates subagent execution via ShadowFS, and manages the append-only event log.

---
## Armature-7

Armature-7: RISC for Build Orchestration

The "7" is a distillation of a minimal agentic actionset inspired by RISC: reduced instruction sets trade flexibility for reliability. CISC architectures offer hundreds of irregular opcodes for programmer convenience. RISC uses minimal, regular primitives that hardware can pipeline and verify. Armature-7 applies this to LLM orchestration.

The manager uses seven operations: dispatch, validate, commit, suspend, seal, consult, halt. This is the minimum for build systems. Any fewer and you encode control flow in data. Any more and the manager becomes a chatbot.

The result is a deterministic state machine: given a BoardView, exactly one valid action exists. No interpretation. No drift between turns.

RISC separates memory access from computation. Armature-7 separates contract definition (test_author) from contract fulfillment (builder). The rigid frame holds exploration in place until tests fuse it into something solid, like an armature holding clay until the kiln fires it.

## Architecture

### Package Structure

```
a7-rt-core/                     # Project root
├── docs/                       # Documentation (project-level)
│   ├── RESTRUCTURE.md          # Restructure history
│   ├── test-map.md             # Import mappings
│   ├── LIVE.md                 # Testing roadmap
│   └── ...
├── evidence/                   # Test sessions and validation data
└── src/a7_rt_core/             # Package source
    ├── __init__.py             # Version 0.1.0
    ├── agent/                  # Agent execution loop
    │   ├── __init__.py
    │   └── loop.py             # AgentLoop - native tool calling
    ├── cli/                    # Command-line interface
    │   ├── __main__.py         # Entry point: a7-rt / a7rt commands
    │   ├── runner.py           # Headless execution
    │   ├── commands/           # init, run, config, seed, stage, stage-reset, narrative, replace
    │   ├── shared/             # Hooks and utilities
    │   └── web/                # Web interface stub
    ├── context/                # View assembly for all roles
    │   ├── __init__.py
    │   └── core.py             # manager_view, builder_view, analyst_view
    ├── core/                   # Core abstractions
    │   ├── __init__.py
    │   ├── models.py           # Pydantic schemas + invariants
    │   ├── graph.py            # Pure graph algorithms
    │   └── config.py           # Hierarchical configuration (.a7 directories)
    ├── data/                   # Bundled package data
    │   ├── __init__.py         # Data access utilities (get_roles_dir, get_protocols_dir)
    │   ├── config.toml.example # Example configuration file
    │   ├── protocols/          # Protocol weight definitions
    │   │   ├── none.md
    │   │   ├── lean.md
    │   │   └── full.md
    │   └── roles/              # LLM prompts
    │       ├── manager_prompt.md
    │       ├── builder.md
    │       ├── test_author.md
    │       └── analyst.md
    ├── harness/                # Core orchestration (mixin architecture)
    │   ├── __init__.py
    │   ├── core.py             # Harness class assembly
    │   ├── control.py          # Lifecycle, routing, chronicle
    │   ├── dispatch.py         # DISPATCH/REDISPATCH handling
    │   ├── commit.py           # COMMIT + git integration
    │   ├── validate.py         # VALIDATE handling
    │   ├── apply.py            # PR application, findings persistence
    │   ├── handlers.py         # SUSPEND, SEAL, CONSULT, UPDATE_PLAN
    │   ├── persistence.py      # Checkpoint, resume, state drain
    │   ├── utils.py            # Metadata extraction
    │   └── ast_adapter.py      # Universal AST: Python + Rust/Go/TS
    ├── llm/                    # LLM layer
    │   ├── __init__.py
    │   ├── client.py           # Shared LLM transport
    │   ├── subagent.py         # LLM dispatch wrapper
    │   ├── a7_engine.py        # A7 CONSULT oracle
    │   └── parser.py           # Manager action parsing
    ├── schemas/                # JSON schemas for tool definitions
    │   ├── __init__.py         # Schema loader utilities
    │   ├── agent/              # Agent tool schemas (OpenAI format)
    │   ├── manager/            # Manager tool schemas
    │   ├── harness/            # Internal harness schemas
    │   └── README.md           # Schema editing guide
    ├── storage/                # Persistence
    │   ├── __init__.py
    │   ├── repository.py       # Atomic I/O for master.json/manager.json
    │   └── shadowfs.py         # Transactional filesystem
    ├── tests/                  # Test suite (pre-release)
    ├── tools/                  # Tool implementations
    │   ├── __init__.py
    │   ├── agent_tools.py      # Tool implementations via ShadowFS
    │   ├── editor.py           # Line-addressed edits
    │   └── mcp.py              # MCP tools integration
    └── validation/             # Validation layer
        ├── __init__.py
        ├── exports.py          # Export validation logic
        └── schema.py           # Return schema validation
```

**Note on Package Data:** The `protocols/` and `roles/` directories are bundled inside the package at `src/a7_rt_core/data/` to ensure reliable access regardless of installation method (editable `pip install -e .` or normal `pip install .`). Access them via:

```python
from a7_rt_core.data import get_roles_dir, get_protocols_dir, read_role_prompt

roles_dir = get_roles_dir()        # Path to bundled roles
protocols_dir = get_protocols_dir() # Path to bundled protocols
builder_prompt = read_role_prompt("builder")  # Read prompt content
```

### Key Interfaces

| Module | Primary Export | Purpose |
|--------|----------------|---------|
| **Core Models** | | |
| `core.models` | `Node`, `ManagerAction` subclasses, `SubagentReturn` | Schema definitions + transition invariants |
| `core.graph` | `poison_set()`, `ready_nodes()`, `cycle_check()` | Pure graph algorithms |
| **Context Assembly** | | |
| `context.core` | `manager_view()`, `builder_view()`, `analyst_view()` | Role-specific context assembly |
| **Orchestration** | | |
| `harness.core` | `Harness`, `HaltSignal` | Session lifecycle + mixin orchestration |
| `storage.repository` | `Repository` | Atomic file I/O, stage/node CRUD |
| `storage.shadowfs` | `ShadowFS` | Transactional workspace |
| **LLM Layer** | | |
| `llm.client` | `LLMClient`, `LLMError` | OpenRouter transport with retry logic |
| `llm.subagent` | `Subagent`, `SubagentError` | Role-specific dispatch wrapper |
| `agent.loop` | `AgentLoop` | Tool-calling agent execution |
| **Validation** | | |
| `validation.schema` | `Validator` | Schema + hard-path test validation |
| `validation.exports` | `validate_exports_in_directory()` | Runtime export verification |
| **Tools** | | |
| `tools.agent_tools` | `AgentTools` | 13 ShadowFS-enabled file tools |
| `harness.ast_adapter` | `extract_symbols()` | Universal AST extraction (Python/Go/TS/Rust) |

---

## Data Model

State is split across two persistent documents and a computed runtime view.

### master.json — Project State

Survives restarts. Contains project configuration, the node graph, and cross-session history.

```json
{
  "project": {
    "project_id": "uuid",
    "name": "string",
    "description": "string",
    "created_at": "ISO8601",
    "stage_ids": ["stage-1"],
    "manager_max_turns": 25,
    "drain_turn": 20,
    "provisional_depth_limit": 2
  },
  "stages": {
    "stage-1": {
      "stage_id": "string",
      "name": "string",
      "status": "active|sealed",
      "node_ids": ["node-a", "node-b"],
      "sealed_at": "ISO8601" | null,
      "summary": "string" | null,
      "exported_interfaces": {"node-id": {"exports": [], "assumptions": []}}
    }
  },
  "nodes": {
    "node_id": {
      "node_id": "string",
      "stage_id": "stage-1",
      "type": "feature|glue|test",
      "status": "near|provisional|grounded|suspended|poisoned",
      "protocol_weight": "none|lean|full",
      "description": "string",
      "interface": {
        "exports": ["signature"],
        "assumptions": ["behavior"],
        "raises": ["exception"],
        "guarantees": ["property"]
      },
      "structural_deps": ["other-node"],
      "assumption_deps": ["other-node"],
      "suspension_reason": {"type": "near|far|void|wild", "detail": "string"} | null,
      "content_file": "path/relative/to/content",
      "created_at": "ISO8601",
      "retry_count": 0,
      "max_retries": 3,
      "retry_context": [{"attempt": 1, "error": "..."}],
      "pr_note": "string" | null,
      "test_contract": "string" | null,
      "committed_files": ["file.py"],
      "tags": [
        {
          "tag_id": "uuid",
          "node_id": "target-node",
          "author_role": "builder|test_author|analyst|human",
          "author_turn": 5,
          "category": "quirk|order|scope|perf|warning",
          "content": "note text (max 200 chars)",
          "propagate": true,
          "confirmed_by": ["builder", "analyst"]
        }
      ],
      "view_spec": {"target_fidelity": "summary|outline|full", "struct_dep_fidelity": "none|signature"},
      "metadata": {
        "tokens": 150,
        "lines": 25,
        "content_hash": "abc123",
        "first_export_preview": "def func():",
        "plumbing_summary": "Exports: func | Imports: os",
        "computed_at": "ISO8601",
        "iteration_history": [5, 8, 12],
        "avg_iterations": 8.33,
        "chronicle": [
          {
            "turn": 5,
            "actor": "manager|agent|analyst",
            "action": "dispatch|return|commit",
            "metadata": {},
            "manager_thought": "string",
            "manager_intent": "string",
            "dispatch_seq": 3,
            "timestamp": "ISO8601"
          }
        ],
        "analyst_findings": [
          {
            "node_id": "target",
            "scope": "node|project|external",
            "findings": ["concern"],
            "confidence": "high|medium|low|void",
            "sources": ["file.py"],
            "created_at": "ISO8601"
          }
        ]
      },
      "metadata_map": {}
    }
  },
  "dependencies": [
    {
      "from_node": "A",
      "to_node": "B",
      "type": "structural|assumption",
      "verified": true,
      "discovered_by": "human|subagent"
    }
  ],
  "graveyard": [
    {"node_id": "dead", "reason": "poisoned|replaced_by:X|stage_reset:Y", "scope": "local|global", "timestamp": "ISO8601"}
  ]
}
```

**Runtime fields (not in Pydantic models):**
- `poisoned_by`: Set during poison propagation to track the root cause node. Only present when `status="poisoned"`.

**Notes:**
- `graveyard`: Tombstone records for deleted/replaced nodes. Global tombstones propagate across stages; local ones are scoped to their stage.
- `retry_context`: Accumulates error history across redispatch attempts.
- `discovered_by`: Tracks whether a dependency was declared by human or inferred by subagent.
- Dependencies are duplicated: node-level `structural_deps`/`assumption_deps` for convenience, root-level `dependencies` array for graph algorithms. Use `repo.add_dependency()` to maintain both.

### manager.json — Session State

Ephemeral session tracking. Present only when a manager is active; deleted when the manager dies or the stage seals.

```json
{
  "current_stage_id": "stage-1",
  "turn": 5,
  "manager_max_turns": 25,
  "drain_turn": 20,
  "mode": "autonomous|drain|dead",
  "in_flight": ["dispatched-node"],
  "human_input_queue": [
    {"action": "slash_command", "detail": "/halt reason", "timestamp": "ISO8601"}
  ],
  "planning": {
    "narrative": "string",
    "checklist": [{"id": "abc123", "text": "...", "status": "pending|in_progress|satisfied|void"}],
    "updated_at": 5
  },
  "checkpoint_at": "ISO8601" | null,
  "wild_counts": {"node-id": 1, "other": 2},
  "pending_commits": {
    "node-id": {"role": "...", "status": "...", "files": [...], "pr_note": "..."}
  }
}
```

**Field notes:**
- `mode`: `autonomous` (normal operation) → `drain` (turn ≥ drain_turn, no new dispatches) → `dead` (turn ≥ max_turns, must SEAL or HALT).
- `wild_counts`: Tracks per-session wild suspensions per node. Two wilds for same node triggers `HaltSignal`.
- `human_input_queue`: Injected events from stdin or analyst escalations. Cleared after each manager turn.
- `checkpoint_at`: Timestamp of last explicit checkpoint (rarely used; harness checkpoints on all state changes).

**Runtime-only fields (not persisted, rebuilt on resume):**
- `pending_returns`: Subagent results awaiting VALIDATE action.
- `validated_returns`: Schema-validated results awaiting COMMIT action.
- `pending_consult`: A7 engine verdict from last CONSULT action (one-turn persistence).
- `lifecycle`: `"new"` (fresh session) or `"resumed"` (restarted from checkpoint).

### Manager Board View

Computed fresh each turn by `manager_view()`. This is what the manager LLM receives—no source code, no full dependency lists, just what's needed to decide the next action.

```json
{
  "lifecycle": "new|resumed",
  "turn": 5,
  "manager_max_turns": 25,
  "drain_turn": 20,
  "mode": "autonomous|drain|dead",
  "in_flight": ["dispatched-node"],
  "active_stage": {"stage_id": "...", "name": "...", "status": "active|sealed"},
  "nodes": {
    "node-id": {
      "node_id": "...",
      "status": "near|provisional|grounded|suspended|poisoned",
      "type": "feature|glue|test",
      "description": "string",
      "interface": {"exports": [], "assumptions": [], "raises": null, "guarantees": null},
      "committed_files": ["file.py"],
      "poisoned_by": null,
      "tokens": 150,
      "exports_preview": "def func():",
      "analyst_findings": {"scope": "...", "confidence": "...", "summary": "...", "count": 1}
    }
  },
  "dependencies": [{"from_node": "A", "to_node": "B", "type": "structural|assumption"}],
  "ready": ["dispatchable-node"],
  "valid_dispatch_targets": ["ready-or-pending"],
  "global_tombstones": [{"node_id": "...", "reason": "...", "scope": "global"}],
  "sealed_stages": [{"stage_id": "...", "name": "...", "summary": "...", "exported_interfaces": {}}],
  "planning": {"narrative": "...", "checklist": [], "updated_at": 5},
  "recent_events": [{"turn": 5, "action": "...", "timestamp": "..."}],
  "pending_returns": {
    "node-id": {"node_id": "...", "awaiting": "validate", "role": "...", "escalate": false, "pr_note": "..."}
  },
  "pending_commits": {
    "node-id": {"node_id": "...", "awaiting": "commit", "role": "...", "status": "...", "pr_note": "..."}
  },
  "pending_consult": {"verdict": "..."} | null,
  "human_input": [{"action": "...", "detail": "..."}],
  "last_error": "string | null",
  "resume_warning": "string | null",
  "context_pressure": 0.35,
  "token_budget": {"allocated": 8000, "consumed": 5200, "nodes_count": 12, "ready_count": 3}
}
```

**Computed fields:**
- `ready` — nodes with all structural deps satisfied (grounded or provisional)
- `valid_dispatch_targets` — ready nodes + poisoned roots (poisoned_by: null) + in-flight/pending nodes that aren't suspended
- `context_pressure` — fraction of token budget consumed (0.0–1.0+)
- `token_budget` — spatial awareness breakdown: allocated budget, consumed tokens, node count, ready count
- `nodes[].tokens` — word count of node content
- `nodes[].exports_preview` — first exported function signature
- `nodes[].analyst_findings` — summarized findings (most recent, with count of total findings)

**Notes:**
- `has_test` is computed at dispatch time in the harness (not in context.py) based on existence of `{node_id}.test` or `{node_id}.py` files
- Node views include full `chronicle` for in-flight nodes; background nodes get summary view only
- Cross-stage tombstones are excluded (only `global` scope visible to manager)

---

## Manager Action Grammar

The manager submits exactly one `submit_action` tool call per turn. The harness routes it, enforces invariants, and returns a new board.

### Control Flow

Check conditions in this order:

1. If `last_error` present → resolve it → stop
2. If `pending_consult` present → read verdict → act → stop
3. If `human_input` non-empty → process → stop
4. If `pending_returns` non-empty → VALIDATE first entry → stop
5. If `pending_commits` non-empty → COMMIT first entry → stop
6. If SEAL conditions met (all nodes resolved, no pending work) → SEAL → stop
7. If `ready` non-empty and `mode` != `drain` → DISPATCH → stop
8. Else → HALT (no valid action) or UPDATE_PLAN (adjust planning state)

**Notes:**
- Token budget overflow raises `BudgetExceeded` → immediate HALT (not gradual pressure thresholds)
- Fan-in auto-trigger: Nodes with 2+ upstream deps get analyst suggestions queued to `human_input`
- `resume_warning` is displayed but does not auto-SUSPEND (manager must decide)

### Action Reference

**DISPATCH**
```json
{
  "action_type": "DISPATCH",
  "parameters": {
    "node_id": "string",
    "role": "builder|test_author|analyst",
    "weight": "none|lean|full",
    "intent": "string",
    "manager_note": "string",
    "focus_hints": ["node-id"],
    "clear_findings": ["node-id"],
    "view_spec": {"target_fidelity": "...", "struct_dep_fidelity": "..."},
    "scope": "node|project|external",
    "query": "string",
    "target_nodes": ["node-id"]
  }
}
```

Constraints:
- `node_id` must be in `ready` — **EXCEPT** when `role: "analyst"` (analyst answers questions about any node)
- Rejected when `mode` is `drain`
- `scope`, `query`, `target_nodes` required when `role: "analyst"`
- `expected_iterations` — soft guidance on anticipated difficulty (default: 5)

**REDISPATCH**
```json
{
  "action_type": "REDISPATCH",
  "parameters": {
    "node_id": "string",
    "role": "builder|test_author|analyst",
    "weight": "none|lean|full",
    "manager_note": "string (required)",
    "intent": "string",
    "clear_findings": ["node-id"],
    "view_spec": {...}
  }
}
```

Constraints:
- Rejected when `mode` is `drain`
- HALT if `wild_counts[node_id] >= 2`
- HALT if `retry_count >= max_retries`
- `manager_note` required: explain what failed and what to do differently

**VALIDATE**
```json
{
  "action_type": "VALIDATE",
  "parameters": {
    "node_id": "string",
    "intent": "string"
  }
}
```

Constraints:
- `node_id` must be in `pending_returns`
- Schema pass → node moves to `pending_commits` (or auto-committed if clean grounded builder)
- Schema fail → node suspended as `wild`, `wild_counts` incremented

**COMMIT**
```json
{
  "action_type": "COMMIT",
  "parameters": {
    "node_id": "string",
    "status": "grounded|provisional|suspended",
    "reason": "string",
    "manager_note": "string",
    "intent": "string",
    "clear_findings": ["node-id"]
  }
}
```

Constraints:
- `node_id` must be in `pending_commits` (VALIDATE must precede)
- Cannot commit `poisoned` — poison propagates automatically
- `protocol_weight: "none"` ceiling is `provisional` (cannot reach `grounded`)
- **test_author special case:** Node stays `near`, `has_test` set to true

**SUSPEND**
```json
{
  "action_type": "SUSPEND",
  "parameters": {
    "node_id": "string",
    "type": "near|far|void|wild",
    "detail": "string",
    "intent": "string"
  }
}
```

Types:
- `near` — One step away; dependency exists but not grounded
- `far` — External data required; await human
- `void` — Epistemic dead end; stop trying
- `wild` — Schema/parse failure; quarantined

**CONSULT**
```json
{
  "action_type": "CONSULT",
  "parameters": {
    "question": "string",
    "relevant_node_ids": ["node-id"],
    "constraints": ["string"],
    "intent": "string"
  }
}
```

Constraints:
- Do not CONSULT twice without reading `pending_consult` verdict
- Use for: contradicting contracts, architecture questions, high-risk decisions


**SEAL**
```json
{
  "action_type": "SEAL",
  "parameters": {
    "summary": "string",
    "intent": "string"
  }
}
```

Preconditions (all must be true):
- No node has status `near`
- No unverified assumption edges (`dependencies` where `verified: false`)
- `pending_returns` is empty
- `pending_commits` is empty
- `provisional` counts as resolved

**SEAL enrichment:** On seal, the harness extracts `export_signatures` from each grounded node's content file using `harness/ast_adapter.py`. This provides type-aware signatures for downstream dependency inference:
- Python: Full type annotations via stdlib `ast`
- Rust/Go/TypeScript: Via tree-sitter (optional dependency)
- Others: Regex fallback

These signatures populate `exported_interfaces` in the sealed stage record.

**HALT**
```json
{
  "action_type": "HALT",
  "parameters": {
    "reason": "string",
    "invariant": "string | null",
    "intent": "string"
  }
}
```

Use when:
- Invariant violated (name it in `invariant` field)
- No valid action exists and not all nodes resolved
- `wild_counts >= 2` and REDISPATCH would be required
- CONSULT verdict contradictory or unhelpful
- `manager_max_turns` approaching with unresolved work

**UPDATE_PLAN**
```json
{
  "action_type": "UPDATE_PLAN",
  "parameters": {
    "narrative": "string",
    "checklist": [{"id": "abc123", "text": "...", "status": "pending|in_progress|satisfied|void"}],
    "intent": "string"
  }
}
```

Purpose: Update manager working memory (narrative + pre-SEAL checklist). Full replacement semantics—manager emits complete new state. Narrative survives SEAL (appended to stage summary); checklist is ephemeral.

Human-as-manager input format: `ACTION node_id [key=value ...]` (for manual override; native tool calling used for LLM)

---

## Nodes and Status Lifecycle

A **node is a capability boundary** with a behavioral contract. It is not a checklist item or project ticket—it is a unit of verified behavior that can be imported, tested, and depended on by other nodes.

**The concreteness test:** Can a competent developer read the description and write the function signatures without asking a follow-up?

| Description | Verdict |
|-------------|---------|
| `"verify_token(token: str) -> dict per RFC 7519"` | ✅ Concrete — explicit signature |
| `"JWT authentication module"` | ❌ Domain label — no surface area |

### Workflow Modes

A7-RT operates in two modes depending on contract maturity:

**Mode A (Contract-First):** `interface.exports` is populated before dispatch. The test_author validates existing signatures. The builder implements against known exports.

```
interface.exports = ["def verify_token(t: str) -> dict"]
  ↓
DISPATCH test_author → validates existing contract → grounded
```

**Mode B (Test-First):** `interface.exports` is empty. The test_author defines the contract via `test_contract`, which the builder implements against.

```
description = "JWT module" (no exports)
  ↓
DISPATCH test_author → writes tests + populates test_contract → provisional
  ↓
DISPATCH builder → implements per test_contract → grounded
```

The `test_contract` field is the **only** communication channel from test_author to builder in Mode B. The builder cannot see test files or pr_notes.

### Node Types

| Type | Purpose | Signal |
|------|---------|--------|
| `feature` | Core capability | Exports behavioral surface |
| `glue` | Coordination/composition | Exports single composition function (`build_app`, `create_session`) |
| `test` | Test suite | Validates other nodes |

**Glue nodes** coordinate N structural dependencies. The signal is the composition function in exports, not the dependency count. A router with 4 handler deps is valid glue; a session manager with 11 deps is not glue—it's a feature node that happens to coordinate.

### Interface Contract

The `interface` field defines the behavioral surface:

- `exports` — Function signatures, classes, or data structures provided
- `assumptions` — Environmental requirements (e.g., "JWT_SECRET env var set")
- `raises` — Exceptions the implementation may raise
- `guarantees` — Behavioral promises

**The `exports` field serves three roles:**
1. **Pre-dispatch:** Tells test_author what surface to test (specification)
2. **Post-commit:** Records what was implemented (documentation)
3. **Post-seal:** Becomes dependency contract for downstream nodes

**The `test_contract` field (Mode B):**
When `exports` is empty, the test_author populates `test_contract` with a behavioral specification. Format:

```
TEST CONTRACT: {node_id}

API:
- def function_name(arg: type) -> return_type

BEHAVIOR:
- Normal case behavior, side effects, priority order

EDGES:
- Empty input → what happens
- Missing file → which exception

COERCION:
- Input transformations (e.g., "true" → bool)
```

This becomes the builder's sole specification. See `roles/test_author.md` for full format.

### Protocol Weight Ceiling

`protocol_weight: "none"` caps status at `provisional`. Nodes with this weight cannot reach `grounded` regardless of test results.

### Status Lifecycle

```
near ──► provisional ──► grounded
 │          │              │
 │          ▼              │
 └──► suspended ◄──────────┘
 │          │
 ▼          │
poisoned ◄──┘ (terminal)
```

**Valid transitions:**

| From | Valid To | Context |
|------|----------|---------|
| `near` | `provisional`, `grounded`, `suspended`, `poisoned` | Initial dispatch or direct commit |
| `provisional` | `grounded`, `suspended`, `poisoned` | Hard-path test passes/fails or blocked |
| `grounded` | `poisoned` | Cross-stage poison (rare) |
| `poisoned` | `near` | Root unpoisoned for retry (REDISPATCH) |
| `suspended` | `near`, `provisional`, `grounded`, `poisoned` | Retry, resolution, or cascade |

**Status definitions:**

- **`near`** — Initial state. Node exists in graph but no validated work yet.
- **`provisional`** — Schema-validated return exists. Code written but hard-path test not yet passed.
- **`grounded`** — Hard-path verified. Compiles, executes, or proves correctly. Terminal for normal operation.
- **`suspended`** — Blocked awaiting external resolution. Carries typed reason: `near`, `far`, `void`, or `wild`.
- **`poisoned`** — Failed or downstream of failure. Terminal status—requires fresh REDISPATCH to recover.

**Poison propagates transitively** through structural and assumption dependencies. When a node is poisoned, all downstream dependents are automatically marked poisoned.

---

## Configuration System

A7-RT uses a hierarchical configuration system with `.a7` directories for project-local settings.

### Directory Layout

```
<project-root>/
├── .a7/                          # A7 configuration directory
│   ├── config.toml               # Main configuration
│   ├── models.toml               # Model aliases and definitions
│   ├── key                       # API key (optional, 0600 permissions)
│   └── projects/                 # Project JSON storage
├── master.json                   # Project state (durable)
├── manager.json                  # Session state (ephemeral)
├── events.jsonl                  # Append-only audit log
└── content/                      # Node work products
```

### Configuration Hierarchy (priority order)

1. **CLI arguments** (`--model`, `--subagent-model`)
2. **Environment variables** (`OPENROUTER_API_KEY`)
3. **Session-local** `.a7/config.toml` (in session directory)
4. **Project-level** `.a7/config.toml` (auto-detected by walking up tree)
5. **User-level** `~/.a7/config.toml`
6. **Built-in defaults**

### config.toml

```toml
# Default models (can use aliases from models.toml)
manager_model = "kimi-k2-5"
subagent_model = "kimi-k2-5"

# Session defaults
default_manager_max_turns = 25
default_drain_turn = 20
provisional_depth_limit = 3

# Provider configuration
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

### models.toml

```toml
[kimi-k2-5]
provider = "openrouter"
model_id = "moonshotai/kimi-k2.5"
max_tokens = 4096
role = ["manager", "subagent"]

[claude-sonnet]
provider = "openrouter"
model_id = "anthropic/claude-sonnet-4-6"
max_tokens = 4096
role = ["manager", "subagent"]
```

### CLI Commands

```bash
# View current configuration
a7-rt config --session=/path/to/session

# List available models
a7-rt config --models

# Initialize .a7 directory
a7-rt config --init --with-key=$OPENROUTER_API_KEY

# Create session with .a7 seeding
a7-rt init /path/to/session

# Run with config file models (no --model needed)
a7-rt run /path/to/session --stage-id=stage-1
```

---

## Agent Execution

The system uses a **looping agent with native tool calling**.

**Execution phases:**

1. **Dispatch** — Manager emits DISPATCH → Harness generates `session_id`, creates ShadowFS, assembles context view (builder_view + focus hints + exploration_hints from prior attempts)

2. **Exploration** — Agent receives context, loops: think → tool call → observe → repeat. Tool calls logged to session file. Max iterations enforced as safety limit.

3. **Submission** — Agent calls `submit_pr` with status, interface, files, pr_note → Harness receives `SubagentReturn` with telemetry

4. **Commit** — Harness runs hard-path test (if grounded), atomically promotes shadow writes to live filesystem, git commits, updates metadata

**Telemetry captured:** `iterations_used`, `tool_usage`, `session_id`. The `pr_note` field (soft limit: 500 chars, hard limit: 1000) carries agent→manager signals.

### ShadowFS: Transactional Filesystem

All agent writes are isolated until `submit_pr`:

- `write_file` — Staged in shadow layer (live untouched)
- `delete_file` — Tombstone in shadow (live untouched)
- `rename_file` — Rename chain in shadow
- `read_file` — Sees shadow writes first, then live fallback
- `run_test` — Executes against staged shadow state

On COMMIT: Shadow state atomically promoted to live filesystem + git commit.

### Agent Toolset (15 Tools)

| Tool | Purpose | Scope |
|------|---------|-------|
| `read_file(path, intent?)` | Full file read with hash header | `content_dir` |
| `grep_content(pattern, intent?)` | Regex search across files | `content_dir` |
| `list_files(glob?, intent?)` | List matching files | `content_dir` |
| `preview_file(path, offset, max_lines, intent?)` | Partial read with content_hash | `content_dir` |
| `create_directory(path, intent?)` | Create directory structure | Shadow |
| `write_file(path, content, intent?)` | Shadow write (full file) | Staged |
| `delete_file(path, intent?)` | Shadow delete | Staged |
| `rename_file(old_path, new_path, intent?)` | Shadow rename | Staged |
| `mv(old_path, new_path, intent?)` | Alias for rename_file | Staged |
| `edit_file(path, content_hash, operations, intent?)` | Line-addressed edits | Staged |
| `edit_files(edits, intent?)` | Batch atomic edits | Staged |
| `run_lint(paths?, language?, intent?)` | Semgrep linting | Shadow state |
| `run_test(intent?)` | Execute tests against shadow state | Shadow state |
| `get_current_state(intent?)` | Summary of shadow state and thoughts | Session |
| `record_thought(thought, category, relates_to?, intent?)` | Structured reasoning log | Session |
| `submit_pr(status, interface, files?, pr_note?, ...)` | Final submission | Returns PR |

**Tool intent parameter:** All tools accept optional `intent` for audit logging.

---

## Context Assembly

Different roles receive different context fidelity:

| Function | Role | Sees | Excludes |
|----------|------|------|----------|
| `manager_view()` | Manager | Full board, planning state, recent events, chronicle | Source code, content_file |
| `builder_view()` | Builder | Target node + structural deps, exploration_hints, manager_note | Assumption dep sources |
| `assemble_test_author_view()` | Test author | Interface contracts, descriptions, dep interfaces | Implementation, source code |
| `analyst_view()` | Analyst | Query-specific nodes (full), dependency subgraph | Nodes outside query scope |

**Architectural boundary:** Test author never sees implementation—only interface contracts. Builder sees source; test author sees signatures.

### Focus Hints (Manager → Agent)

DISPATCH injects guidance via `focus_hints`:

```python
{
    "focus_on": ["node-id", "other-node"],  # Emphasize these deps
    "manager_note": "Check error handling",  # Strategic context
    "expected_iterations": 5                 # Difficulty signal (5=simple, 15=complex)
}
```

### Exploration Hints (Warm Redispatch)

Redispatched nodes receive prior attempt summary:

```python
{
    "previous_attempts": 3,
    "files_written": ["auth/jwt.py", "auth/config.py"]
}
```

Prevents redundant exploration across retries.

---

## Debugging

| Symptom | Investigation | Location |
|---------|---------------|----------|
| "Node has active session" | Check orphaned session | `content/.sessions/{node_id}.jsonl` |
| Poison cascade | Trace downstream deps | `master.json` dependencies |
| Commit fails validation | Review test output | `events.jsonl` — look for `run_test` |
| Agent loops forever | Check iteration history | `node.metadata.iteration_history` |
| Context too large | Adjust view fidelity | `node.view_spec` |
| Resume after crash | Verify session state | `manager.json` wild_counts |

**Session logs** (`content/.sessions/{node_id}.jsonl`):
```jsonl
{"type": "session_start", "session_id": "...", "node_id": "...", "role": "...", "dispatch_turn": 5}
{"type": "tool_call", "tool": "read_file", "args": {"path": "auth/jwt.py"}}
{"type": "thought", "thought": "jwt.py has no verify()", "category": "contradiction"}
{"type": "session_end", "status": "completed", "iterations_used": 4}
```

**Chronicle** vs **Session logs:**
- Session logs: Per-dispatch, diagnostic only, cleared on SEAL
- Chronicle: In `node.metadata.chronicle`, persists across dispatches

**Commands:**
```bash
# View session narrative
a7-rt narrative /tmp/session --node=auth.jwt

# Check node status
python -c "import json; d=json.load(open('master.json')); print(json.dumps(d['nodes']['auth.jwt'], indent=2))"

# Trace recent events
tail -50 events.jsonl | python -m json.tool
```

---

## Development

### Running Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_graph.py -v
```

---

## License

Apache 2.0
