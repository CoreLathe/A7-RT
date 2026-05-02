# A7-RT Schema Registry

Centralized schema definitions for A7-RT. All schemas are stored as JSON for easy editing without code changes.

⚠️ **Warning: Schema-Code Coupling**

While these schemas are editable as JSON, many changes **require corresponding code updates**:

- Adding/removing tool parameters → Update parsing logic in `agent/loop.py`, `llm/parser.py`
- Changing action types → Update harness handlers in `harness/*.py`
- Modifying validation rules → Update `validation/schema.py` logic
- Changing state transitions → Update `core/models.py` invariants

Schemas define the **interface contract**; code implements the **behavior**. Keep them in sync.

## Directory Structure

```
schemas/
├── agent/
│   └── tools.json           # 13 OpenAI function schemas for agent loop
├── manager/
│   └── tools.json           # Manager submit_action tool + tool_choice
├── harness/
│   ├── validation.json      # Role-based field validation rules
│   ├── state_transitions.json  # Node status transition table
│   └── returns/
│       ├── builder.json     # Builder subagent return schema
│       ├── test_author.json # Test author return schema
│       └── analyst.json     # Analyst return schema
└── __init__.py              # Loader utilities
```

## Editing Schemas

All JSON files can be edited directly. Changes take effect on next Python import/module reload.

### Agent Tools (`agent/tools.json`)
- 13 OpenAI function calling schemas
- Edit descriptions, add parameters, modify enums
- The `submit_pr` schema is large; edit carefully

### Manager Tools (`manager/tools.json`)
- Single `submit_action` tool with all action types
- `tool_choice` forces the model to call submit_action

### Validation Rules (`harness/validation.json`)
- `required_fields`: Fields that must be present per role
- `nonnull_fields`: Fields that must be non-None per role
- Used by `validation/schema.py`

### State Transitions (`harness/state_transitions.json`)
- Valid status transitions for nodes
- Used by `core/models.py` for transition validation

### Return Schemas (`harness/returns/*.json`)
- JSON Schema format for subagent return validation
- Used for documentation and potential future runtime validation

## Usage in Code

```python
# Load schemas dynamically
from a7_rt_core.schemas import load_agent_tools, load_manager_tools

tools = load_agent_tools()  # Returns list of 13 tool schemas

# Or use pre-loaded constants (backward compatible)
from a7_rt_core.schemas import AGENT_TOOL_SCHEMAS, MANAGER_TOOLS

# In your module
response = llm.call(
    messages=messages,
    tools=AGENT_TOOL_SCHEMAS,  # From schemas/agent/tools.json
)
```

## Versioning

Each schema file includes a `version` field. Bump version when making breaking changes.

## Validation

Run tests after schema changes:
```bash
cd a7_rt_core && python -m pytest src/a7_rt_core/tests/ -v
```

## Guidelines

1. **Descriptions matter**: LLMs read these. Be clear and specific.
2. **Enums are constraints**: Only add values the harness can handle.
3. **Required fields**: Changing these affects validation logic.
4. **Test locally**: Schema errors can break the agent loop.