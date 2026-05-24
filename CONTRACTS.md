
# Writing A7-RT Contracts

Contracts are how you tell the system what to build without micromanaging the implementation. You declare the interface—what goes in, what comes out, what promises are kept—and the agents handle the rest.

## The Minimal Contract

At its simplest, a contract needs three things:

```json
{
  "node_id": "hello.world",
  "type": "feature",
  "description": "Returns a greeting string",
  "exports": ["hello_world() -> str"]
}
```

Save this somewhere, then seed it into a session:

```bash
a7-rt seed /tmp/my-session --nodes='{
  "node_id": "hello.world", 
  "type": "feature", 
  "description": "Returns a greeting", 
  "exports": ["hello_world() -> str"]
}'
```

Run it:

```bash
a7-rt run /tmp/my-session --stage-id=stage-1
```

That's it. The system now knows it needs to produce a function called `hello_world` that returns a string. It does not care how.

## Two Ways to Write the Same Thing

The CLI accepts two formats. Use whichever feels right.

**Flat format** (good for command line, less typing):

```json
{
  "node_id": "parser.csv",
  "type": "feature",
  "description": "Parses CSV strings into list of dicts",
  "exports": ["parse_csv(data: str) -> list[dict]"],
  "assumptions": ["types.row exports Row"],
  "guarantees": ["Returns empty list for empty input"],
  "raises": ["CSVError: malformed data"],
  "deps": ["types.row", "utils.encoding"]
}
```

**Nested format** (how it gets stored internally):

```json
{
  "node_id": "parser.csv",
  "type": "feature",
  "description": "Parses CSV strings into list of dicts",
  "interface": {
    "exports": ["parse_csv(data: str) -> list[dict]"],
    "assumptions": ["types.row exports Row"],
    "guarantees": ["Returns empty list for empty input"],
    "raises": ["CSVError: malformed data"]
  },
  "structural_deps": ["types.row", "utils.encoding"]
}
```

The CLI handles both. Flat is usually easier when you are typing at a terminal. Nested is what you see if you peek at the session files later.

## What Each Field Actually Means

**node_id**  
A unique name. Use dots to show hierarchy: `parser.core`, `auth.jwt`, `types.user`. Keep it descriptive because you will reference it in other contracts.

**type**  
Three options:
- `feature` — actual functionality (parsers, APIs, business logic)
- `glue` — wiring things together (format converters, middleware)
- `test` — verification nodes that check other nodes

**description**  
One sentence saying what this does. Not how it works, just what it achieves.  
Good: "Parses ISO 8601 dates into datetime objects"  
Bad: "Uses regex to split on T character"

**exports**  
What the outside world can call. These are public API signatures. Do not put internal helpers here.

```python
# Examples
"parse_date(iso_string: str) -> datetime"
"validate_token(token: str) -> bool"
"UserRepository.find_by_id(id: UUID) -> User | None"
```

**deps**  
Other node_ids this one needs to function. If your CSV parser needs the Row type and encoding utils, list them:

```json
"deps": ["types.row", "utils.encoding"]
```

**assumptions**  
What you expect from those dependencies. This is how nodes communicate without reading each other's code.

```json
"assumptions": [
  "types.row exports Row dataclass",
  "utils.validators raises ValidationError on malformed input"
]
```

**guarantees**  
What you promise to consumers of this node. Be specific about behavior, not implementation.

```json
"guarantees": [
  "Returns None for empty input",
  "Never raises on valid ISO 8601 strings",
  "O(1) lookup for cached results"
]
```

**raises**  
Exceptions callers should actually handle. Do not list internal errors that get caught and transformed.

```json
"raises": [
  "ValidationError: malformed input",
  "TimeoutError: upstream service unavailable >5s"
]
```

## When to Leave Exports Empty

Sometimes you know what you want something to do, but you do not know what the API should look like yet. That is fine. Leave `exports` empty and focus on `guarantees`.

```json
{
  "node_id": "matcher.engine",
  "type": "feature",
  "description": "Matches requests against rules, returns first match",
  "exports": [],
  "guarantees": [
    "First matching rule wins",
    "O(n) where n is number of rules"
  ]
}
```

The test author will define the contract through tests. The API surface emerges from what actually gets built. This is Mode B (Test-First). The normal case with filled exports is Mode A (Contract-First). Both work.

## The Difference Between Feature and Glue

This trips people up. 

A **feature** exports behavior—functions that actually do work. A **glue** exports a single composition function that wires other features together.

Glue examples: `build_app()`, `create_pipeline()`, `initialize_system()`. If the main export sounds like "put the pieces together," it is glue. Everything else is probably a feature.

## Common Mistakes

**Exporting internals**  
Do not put helper functions in exports. If it starts with an underscore or is only used internally, it does not belong here. Exports are for consumers.

**Describing implementation**  
"Uses Redis for caching" belongs in code comments, not guarantees. Guarantees should say "Returns cached result if available"—the how is up to the implementation.

**Vague types**  
Be specific. `find_user(id: UUID) -> User | None` is better than `find_user(id) -> user`.

## Full Examples

### Feature: CSV Parser

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

Note that test nodes validate the contract surface, not the implementation. They verify that `parser.csv` actually exports what it promised, regardless of how it was written.

## Quick Reference

| Field | Purpose |
|-------|---------|
| node_id | Unique identifier, use dots |
| type | feature, glue, or test |
| description | What it does, not how |
| exports | Public API signatures |
| deps | Node IDs this needs |
| assumptions | What you expect from deps |
| guarantees | What you promise consumers |
| raises | Exceptions to handle |

Start with a minimal contract. Add assumptions and guarantees as you discover what actually matters. The system works fine with sparse contracts—you can always tighten them later.
