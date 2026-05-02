# A7-RT CLI Usage Guide

**Audience:** Engineers and LLM agents using A7-RT via command line  
**Scope:** CLI package (`a7-rt` command or `python -m a7_rt_core.cli`) 
**Status:** Current as of A7-RT core implementation

> **Note on command syntax:** After `pip install -e .`, use `a7-rt` (or `a7rt`). For development without installation, use `python -m a7_rt_core.cli` from the project root.

---

## Quick Start

```bash
# Initialize session (uses ~/.a7/config.toml or project .a7/config.toml)
a7-rt init /tmp/my-session

# Add nodes
a7-rt seed /tmp/my-session --nodes='{"id":"auth","type":"feature"}'

# Create stage
a7-rt stage /tmp/my-session --name=core

# Run
a7-rt run /tmp/my-session --stage-id=stage-1 --turn-limit=50

# Review
a7-rt narrative /tmp/my-session
```

> **Development mode:** Replace `a7-rt` with `python -m a7_rt_core.cli` if running from source without installation.

---

## Command Reference

### `init` — Initialize Session

Create a new A7-RT session directory with `master.json` and content structure.

```bash
a7-rt init <session-path> [options]
```

**Options:**
- `--source=<path>` — Source directory (informational, no auto-import)
- `--dry-run` — Preview without creating files
- `--name=<name>` — Session name (default: directory name)
- `--manager-max-turns=<N>` — Default max turns for manager (default: 200)
- `--drain-turn=<N>` — Default drain turn (default: 40)
- `--help, -h` — Show help

**Examples:**
```bash
# Blank slate (uses user/project config)
a7-rt init /tmp/s

# Named session with limits
a7-rt init /tmp/s --name="Auth Service" --manager-max-turns=200

# Dry run
a7-rt init /tmp/s --dry-run
```

**Output:**
- `master.json` — Project state
- `content/` — Implementation directory
- `events.jsonl` — Event log (empty)
- `content/.sessions/` — Session logs directory

**Config Inheritance:**
Sessions do NOT create `.a7/` automatically. Config is loaded from (priority order):
1. Session-local `.a7/config.toml` (if you create it manually)
2. Project-level `.a7/config.toml` (if session is within a project)
3. User-level `~/.a7/config.toml` (your personal defaults)

To create a session-specific config:
```bash
a7-rt init /tmp/s
a7-rt config --init=/tmp/s --manager-model=kimi-k2-5
```

---

### `seed` — Add Nodes

Add nodes to an existing A7-RT session.

```bash
a7-rt seed <session-path> [options]
```

**Options:**
- `--nodes=<json>` — Node definition as JSON (repeatable)
- `--nodes-file=<path>` — JSON file containing array of nodes
- `--spec=<source>` — Batch spec: file path, JSON string, or `-` for stdin
- `--deps=<json>` — Dependency edge as JSON (repeatable)
- `--deps-file=<path>` — JSON file containing array of dependency edges
- `--help, -h` — Show help

**Node JSON Format:**
```json
{
  "id": "node-id",
  "type": "feature|glue|test",
  "description": "What this node does",
  "deps": ["other-node-1", "other-node-2"],
  "stage": "stage-id"
}
```

**Dependency JSON Format:**
```json
[
  {"from": "node-a", "to": "node-b", "type": "structural"},
  {"from": "node-c", "to": "node-d", "type": "structural"}
]
```

**Examples:**
```bash
# Single node
a7-rt seed /tmp/s --nodes='{"id":"auth","type":"feature"}'

# Multiple nodes
a7-rt seed /tmp/s \
  --nodes='{"id":"auth","type":"feature"}' \
  --nodes='{"id":"auth.test","type":"test","deps":["auth"]}'

# From file
a7-rt seed /tmp/s --nodes-file=nodes.json

# From stdin
echo '[{"id":"api","type":"glue"}]' | a7-rt seed /tmp/s --spec=-

# With dependencies from file
a7-rt seed /tmp/s --spec=nodes.json --deps-file=deps.json

# Inline dependency
a7-rt seed /tmp/s \
  --nodes='{"id":"api","type":"glue"}' \
  --nodes='{"id":"auth","type":"feature"}' \
  --deps='{"from":"api","to":"auth","type":"structural"}'
```

**Notes:**
- Nodes are added with `status: "near"` (ready for dispatch)
- Stage is created automatically if it doesn't exist (from node "stage" field)
- Per-node `deps` populate `structural_deps` for that node
- Global `--deps` and `--deps-file` add edges to the dependency graph

---

### `replace` — Replace Node

Replace a suspended or poisoned node with a new implementation. Useful for contract revision when a node's interface is void or needs fundamental redefinition.

```bash
a7-rt replace <session-path> <old-node> <new-node> [options]
```

**Options:**
- `--redirect-deps` — Update all nodes that depend on old-node to point to new-node
- `--copy-interface` — Copy interface.exports from old-node to new-node
- `--dry-run` — Preview changes without applying
- `--force` — Allow replacement even if old-node is active (not suspended/poisoned)
- `--help, -h` — Show help

**Examples:**
```bash
# Replace suspended admin.api with admin.api.v2
a7-rt replace /tmp/session admin.api admin.api.v2 --redirect-deps

# Preview what would change
a7-rt replace /tmp/session auth.v1 auth.v2 --dry-run

# Copy interface and redirect all dependencies
a7-rt replace /tmp/session old.node new.node --copy-interface --redirect-deps
```

**Workflow:**
1. Create replacement node with `seed`:
   ```bash
   a7-rt seed /tmp/session --nodes='{"id":"admin.api.v2","type":"glue","stage":"integration"}'
   ```
2. Run replace to redirect dependencies:
   ```bash
   a7-rt replace /tmp/session admin.api admin.api.v2 --redirect-deps
   ```
3. Resume the session:
   ```bash
   a7-rt run /tmp/session --stage-id=integration
   ```

**Notes:**
- Old node must be `suspended` or `poisoned` (use `--force` to override)
- New node must already exist in the session
- `--redirect-deps` updates both the dependency graph and each dependent node's `structural_deps`
- Old node is added to graveyard with `replaced_by` metadata for audit trail

---

### `stage` — Create Stage

Create a new stage in an existing session.

> **Note:** This command is optional. Stages are automatically created from the `"stage"` field in node specs during `seed`. Use this command only if you need explicit stage creation before seeding nodes.

```bash
a7-rt stage <session-path> [options]
```

**Options:**
- `--name=<name>` — Stage name (default: stage-N)
- `--stage-id=<id>` — Explicit stage ID (default: auto-generated)
- `--help, -h` — Show help

**Examples:**
```bash
# Auto-named stage
a7-rt stage /tmp/s

# Named stage
a7-rt stage /tmp/s --name=api

# Explicit ID
a7-rt stage /tmp/s --stage-id=api-v2 --name="API Layer"
```

---

### `run` — Headless Execution

Run an A7-RT session.

```bash
a7-rt run <session-path> [options]
```

**Core Options:**
- `--stage-id=<id>` — Stage to run (default: first active stage)
- `--turn-limit=<N>` — Maximum runner turns (default: 200, 0=unlimited)
- `--context-mode=<mode>` — Subagent context mode: `accumulate` (default) or `fresh` (experimental)
- `--daemon` — Run continuously until complete/halted
- `--model=<name>` — Manager model override
- `--subagent-model=<name>` — Subagent model override

**Context Mode:**
- `accumulate` (default) — Agent accumulates context within a dispatch (conversation history)
- `fresh` (experimental) — Agent gets fresh context each iteration (stateless, harness-managed)

**Telemetry Options:**
- `--emit-board` — Emit manager board view to stderr (JSONL)
- `--emit-raw` — Emit raw LLM responses to stderr (JSONL)
- `--emit-subagent-raw` — Emit subagent responses to stderr (JSONL)

**Reserved Options:**
- `--stdin-input` — Accept human commands via stdin (reserved for web viewer)

**Examples:**
```bash
# Basic run (default accumulate context mode)
a7-rt run /tmp/s --stage-id=stage-1

# With telemetry
a7-rt run /tmp/s --emit-board --emit-raw 2>telemetry.jsonl

# Experimental fresh context mode
a7-rt run /tmp/s --stage-id=stage-1 --context-mode=fresh

# Daemon mode
a7-rt run /tmp/s --stage-id=stage-1 --daemon --turn-limit=200

# Model override
a7-rt run /tmp/s --model=anthropic/claude-sonnet-4-6
```

**Telemetry Output:**
```jsonl
{"turn": 1, "event": "board", "board": {...}}
{"turn": 1, "event": "raw_response", "response": "..."}
{"turn": 1, "event": "subagent_raw", "node_id": "...", "data": {...}}
```

**Persistent Storage:**
- `content/.sessions/{node_id}.jsonl` — Per-node session logs (always written)
- `{session}/events.jsonl` — Global events (always written)

**Reserved Flags:**
- `--stdin-input` — Reserved for web viewer integration. Currently has no effect in headless CLI mode.

---

### `narrative` — View Session History

Reconstruct and display session narrative.

```bash
a7-rt narrative <session-path> [options]
```

**Options:**
- `--max-turns=<N>` — Limit narrative to first N turns (event filtering)
- `--node=<node_id>` — Filter to specific node
- `--raw` — Output raw events as JSONL
- `--help, -h` — Show help

**Examples:**
```bash
# Full narrative
a7-rt narrative /tmp/s

# First 20 turns
a7-rt narrative /tmp/s --max-turns=20

# Specific node
a7-rt narrative /tmp/s --node=auth.handler

# Export raw
a7-rt narrative /tmp/s --raw > events.jsonl
```

---

### `config` — Configuration Management

View and manage A7-RT configuration. Configuration is file-driven via `config.toml` - no hardcoded providers or models.

```bash
a7-rt config [options]
```

**Options:**
- `--session=<path>` — Show config for specific session
- `--init[=<path>]` — Initialize `.a7` directory (default: current dir)
- `--with-key=<key>` — API key to store (with `--init`)
- `--manager-model=<name>` — Set manager model (with `--init`)
- `--subagent-model=<name>` — Set subagent model (with `--init`)
- `--models` — List configured models from config.toml
- `--providers` — List configured providers from config.toml
- `--validate` — Validate configuration and report errors
- `--help, -h` — Show help

**Examples:**
```bash
# Show current config
a7-rt config

# Validate configuration
a7-rt config --validate

# Initialize .a7 directory with models
a7-rt config --init --manager-model=gpt-4o --subagent-model=gpt-4o-mini

# With API key
a7-rt config --init=/path --with-key=sk-...

# List configured models
a7-rt config --models
```

---

### Configuration File Structure

A7-RT uses a single `config.toml` file with provider and model definitions. Copy `config.toml.example` as a starting point:

```bash
cp config.toml.example .a7/config.toml
```

**Minimal config.toml:**
```toml
# Role-to-model mapping
manager_model = "claude-sonnet"
subagent_model = "claude-haiku"

# Provider configuration
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key = "sk-or-..."  # Or use api_key_env = "OPENROUTER_API_KEY"

# Model definitions (reference providers above)
[models.claude-sonnet]
provider = "openrouter"
model_id = "anthropic/claude-sonnet-4-6"
max_tokens = 4096

[models.claude-haiku]
provider = "openrouter"
model_id = "anthropic/claude-haiku-4-5"
max_tokens = 4096
```

**Key principles:**
- **No hardcoded providers** — All providers defined in your config.toml
- **No hardcoded models** — All models defined in your config.toml
- **Role-to-model mapping** — Set `manager_model` and `subagent_model` to reference `[models.X]` sections
- **Hierarchical loading** — User `~/.a7/config.toml` → Project `.a7/config.toml` → Session `.a7/config.toml`

### `web` — Web Interface (Stub)

Web interface is a planned feature. Currently displays stub message.

```bash
a7-rt web
```

**Output:**
```
A7-RT Web Interface
The web interface is a planned feature and not yet implemented.
Please use the CLI commands instead.
```

---

## Workflow Patterns

### Pattern 1: Contract-First Development

```bash
# 1. Initialize
a7-rt init /tmp/auth-service --name="Auth Service"

# 2. Seed contract (stages auto-created from "stage" field)
a7-rt seed /tmp/auth-service \
  --nodes='{"id":"jwt","type":"feature","description":"JWT token handling","stage":"core"}' \
  --nodes='{"id":"jwt.test","type":"test","deps":["jwt"],"stage":"core"}'

# 3. Run verification
a7-rt run /tmp/auth-service --stage-id=core --turn-limit=50

# 4. Review
a7-rt narrative /tmp/auth-service
```

### Pattern 2: Batch Import from Spec

```bash
# Create spec file nodes.json
cat > nodes.json << 'EOF'
[
  {"id":"types.core","type":"feature","description":"Core types","stage":"core"},
  {"id":"matcher.parser","type":"feature","description":"Pattern parser","deps":["types.core"],"stage":"core"},
  {"id":"matcher.engine","type":"feature","description":"Matching engine","deps":["matcher.parser"],"stage":"core"}
]
EOF

# Create deps file (optional — deps can be in node specs)
cat > deps.json << 'EOF'
[
  {"from":"matcher.parser","to":"types.core","type":"structural"},
  {"from":"matcher.engine","to":"matcher.parser","type":"structural"}
]
EOF

# Initialize and seed (auto-creates stages from node specs)
a7-rt init /tmp/parser
a7-rt seed /tmp/parser --spec=nodes.json --deps-file=deps.json

# Run specific stage
a7-rt run /tmp/parser --stage-id=core
```

### Pattern 3: CI/CD Pipeline

```bash
#!/bin/bash
set -e

SESSION="/tmp/ci-$(date +%s)"

# Setup
a7-rt init "$SESSION" --yes
a7-rt seed "$SESSION" --spec=project-nodes.json

# Run with telemetry
a7-rt run "$SESSION" \
  --stage-id=stage-1 \
  --turn-limit=200 \
  --no-consult \
  --emit-board 2>telemetry.jsonl

# Check results
a7-rt narrative "$SESSION" --raw | jq '.status' | grep -q "sealed"
```

### Pattern 4: Multi-Stage Workflow

```bash
SESSION="/tmp/multi"

# Init
a7-rt init "$SESSION" --yes

# Stage 1: Core (nodes and deps in single spec)
a7-rt seed "$SESSION" --spec='[
  {"id":"config","type":"feature","stage":"infra","deps":[]},
  {"id":"db","type":"feature","deps":["config"],"stage":"infra"}
]'
a7-rt run "$SESSION" --stage-id=infra --turn-limit=30

# Stage 2: API (depends on sealed infra)
a7-rt seed "$SESSION" --spec='[
  {"id":"routes","type":"glue","deps":["db"],"stage":"api"}
]'
a7-rt run "$SESSION" --stage-id=api --turn-limit=30
```

### Pattern 5: Complex Graph with Separate Dependencies

For large projects, keep node specs and dependency edges in separate files:

```bash
SESSION="/tmp/complex"

# Init
a7-rt init "$SESSION" --name="Complex Service"

# Seed nodes (stages auto-created from "stage" field)
a7-rt seed "$SESSION" --spec=nodes.json

# Add cross-cutting dependencies separately
a7-rt seed "$SESSION" --deps-file=cross-cutting-deps.json

# Run core stage
a7-rt run "$SESSION" --stage-id=core --turn-limit=200
```

---

## Exit Codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Session sealed or completed |
| 1 | Error | Check stderr for details |
| 130 | Interrupted | User pressed Ctrl+C |

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | API key for OpenRouter (if not using `.a7/key`) |
| `A7_CONFIG_PATH` | Override config file location |

---

## File Structure

```
/tmp/my-session/              # Session directory
├── master.json               # Project state (nodes, stages, deps)
├── manager.json              # Runtime state (if running)
├── events.jsonl              # Global event log
└── content/                  # Implementation files
    ├── .sessions/            # Per-node session logs
    │   ├── node-a.jsonl
    │   └── node-b.jsonl
    ├── node-a.py             # Node implementations
    └── node-b.py
```

---

## Comparison: CLI vs TUI

| Feature | CLI (`a7-rt`) | TUI (`a7-rt-tui` or `python -m a7_rt_core.tui`) |
|---------|---------------|------------------------------------------------|
| Interface | Command-line | Interactive terminal UI |
| Best for | Scripts, CI/CD, automation | Exploration, debugging |
| Telemetry | `--emit-*` flags to stderr | Built-in visualization |
| Human input | `--daemon` or manual | Interactive prompts |
| Web future | Planned stub | N/A |

> **Note:** Use `python -m a7_rt_core.cli` instead of `a7-rt` for development without installation.

---

## Troubleshooting

### "No session found"

Ensure `master.json` exists:
```bash
ls /tmp/my-session/master.json || echo "Initialize first: a7-rt init /tmp/my-session"
```

### "No active stage found"

Create a stage:
```bash
a7-rt stage /tmp/my-session --name=default
```

### Import errors

Ensure `a7-rt-core` is installed (`pip install -e .`) or use `python -m a7_rt_core.cli` from the project root with `PYTHONPATH` set.

---

## See Also

- `CONTRACTS.md` — Contract semantics and patterns
- `README.md` — System overview
