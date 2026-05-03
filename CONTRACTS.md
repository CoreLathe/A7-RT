# A7-RT Contract Reference

Contracts declare intent separately from implementation. They enable AI agents to write code that composes correctly by reading what other nodes promise, not by reading their code.

---

## Quick Start

A minimal contract (flat format):

```json
{
  "node_id": "hello.world",
  "type": "feature",
  "description": "Returns a greeting string",
  "exports": ["hello_world() -> str"]
}
```

Add it to a session:

```bash
a7-rt seed /tmp/my-session --nodes='{"node_id": "hello.world", "type": "feature", "description": "Returns a greeting", "exports": ["hello_world() -> str"]}'
```

Run the build:

```bash
a7-rt run /tmp/my-session --stage-id=stage-1
```

**Note:** The CLI accepts both flat format (`exports`, `assumptions`, etc.) and nested format (`interface.exports`). Flat is recommended for brevity.

---

## Schema Reference

The CLI accepts two formats. **Flat format** is recommended for command-line use.

### Flat Format (Recommended)

```json
{
  "node_id": "parser.csv",
  "type": "feature",
  "description": "Parses CSV strings",
  "exports": ["parse_csv(data: str) -> list[dict]"],
  "assumptions": ["types.row exports Row"],
  "guarantees": ["Returns empty list for empty input"],
  "raises": ["CSVError: malformed data"],
  "deps": ["types.row", "utils.encoding"]
}
```

### Nested Format (Storage/Internal)

```json
{
  "node_id": "parser.csv",
  "type": "feature",
  "description": "Parses CSV strings",
  "interface": {
    "exports": ["parse_csv(data: str) -> list[dict]"],
    "assumptions": ["types.row exports Row"],
    "guarantees": ["Returns empty list for empty input"],
    "raises": ["CSVError: malformed data"]
  },
  "structural_deps": ["types.row", "utils.encoding"]
}
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | string | Unique identifier. Use dot notation: `module.submodule.name` |
| `type` | enum | `feature`, `glue`, or `test` |
| `description` | string | What this node does. One sentence. |
| `exports` | string[] | Public API signatures (flat format) |
| `assumptions` | string[] | What this node needs from dependencies (flat format) |
| `guarantees` | string[] | Behavioral promises (flat format) |
| `raises` | string[] | Exceptions this node may raise (flat format) |
| `deps` | string[] | Node IDs this node imports from (flat format) |

**Flat vs Nested:** The CLI accepts `exports` (flat) or `interface.exports` (nested). Use flat for command-line brevity. Both store as nested internally.

### Field Details

#### `node_id`
- Unique within project
- Dot notation recommended: `parser.core`, `auth.oauth`, `types.user`
- Used for dependency references and file naming

#### `type`

| Type | Purpose | Example |
|------|---------|---------|
| `feature` | Core functionality | Parser, API endpoint, data model |
| `glue` | Adapter/bridge | Format converter, middleware wrapper |
| `test` | Validation node | Contract test, integration test |

#### `description`
- Single sentence stating purpose
- Not implementation details
- Good: "Parses ISO 8601 dates into datetime objects"
- Bad: "Uses regex to split on T character"

#### `exports`
Array of signatures. Format depends on language:

```python
# Python
"parse_date(iso_string: str) -> datetime"
"validate_token(token: str) -> bool"
"UserRepository.find_by_id(id: UUID) -> User | None"
```

#### `deps`
List of node IDs this node statically imports:

```json
"deps": ["types.core", "utils.validators"]
```

#### `assumptions`
What this node requires from its dependencies:

```json
"assumptions": [
  "types.core exports ISO8601_REGEX",
  "utils.validators raises ValidationError on malformed input"
]
```

#### `guarantees`
Behavioral promises:

```json
"guarantees": [
  "Returns None for empty input",
  "Never raises on valid ISO 8601 strings",
  "O(1) lookup for cached results"
]
```

#### `raises`
Exceptions callers should handle:

```json
"raises": [
  "ValidationError: malformed input",
  "TimeoutError: upstream service unavailable >5s"
]
```

---

## Examples by Type

### Feature: Data Parser

```json
{
  "node_id": "parser.csv",
  "type": "feature",
  "description": "Parses CSV strings into list of dicts",
  "exports": [
    "parse_csv(data: str, headers: list[str] | None = None) -> list[dict]",
    "CSVError: Exception"
  ],
  "assumptions": [
    "types.row exports Row dataclass",
    "utils.encoding handles UTF-8 and UTF-16"
  ],
  "guarantees": [
    "Returns empty list for empty input",
    "Respects headers parameter if provided",
    "Raises CSVError on malformed rows"
  ],
  "raises": [
    "CSVError: unescaped quotes, inconsistent column count"
  ],
  "deps": ["types.row", "utils.encoding"]
}
```

### Glue: API Adapter

```json
{
  "node_id": "adapter.stripe",
  "type": "glue",
  "description": "Wraps Stripe API for internal payment types",
  "exports": [
    "create_payment_intent(amount: Money) -> PaymentResult",
    "refund_charge(charge_id: str) -> RefundResult"
  ],
  "assumptions": [
    "STRIPE_API_KEY available in environment",
    "types.payment exports Money dataclass with currency validation"
  ],
  "guarantees": [
    "Converts all Stripe errors to PaymentError",
    "Logs transaction IDs for audit trail"
  ],
  "raises": [
    "PaymentError: card declined, invalid currency, network failure"
  ],
  "deps": ["types.payment", "config.secrets"]
}
```

### Test: Contract Validation

```json
{
  "node_id": "test.parser.csv",
  "type": "test",
  "description": "Validates parser.csv contract compliance",
  "exports": ["run_tests() -> TestReport"],
  "guarantees": [
    "Tests empty input returns empty list",
    "Tests headers parameter respected",
    "Tests CSVError raised on malformed input"
  ],
  "deps": ["parser.csv"]
}
```

Test nodes validate the contract surface, not the implementation. They verify that `parser.csv` actually exports what it promises, regardless of how it's implemented.

---

## Interface Writing Tips

**Be specific about types:**
- ✅ `find_user(id: UUID) -> User | None`
- ❌ `find_user(id) -> user`

**State behavior, not implementation:**
- ✅ "Returns cached result if available"
- ❌ "Checks Redis before database"

**Document edge cases:**
- ✅ "Returns empty string for None input"
- ✅ "Raises ValueError for negative integers"

**Limit exports:**
- Public API only. Internal helpers don't appear in `exports`.

---
