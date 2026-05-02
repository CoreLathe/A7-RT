# Running Tests

Quick verification that the A7-RT Stage 1 core layer is grounded and functional.

## Prerequisites

```bash
pip install pytest
```

## Stage 1: Core Layer (Complete)

Test the 8 core feature nodes:

```bash
# Run all core tests (47 tests)
pytest

# Or verbose with timing
pytest -v

# Quick smoke test (imports all modules)
python test_all.py
```

### Test Organization

```
types/
├── __init__.py
├── core.py           # Request, Response, MatchRule, Template, Session
└── test_core.py      # 15 tests

matcher/
├── __init__.py
├── parser.py         # Pattern compilation, MatchRule parsing
├── engine.py         # MatcherEngine with collision detection
├── test_parser.py    # 20 tests
└── test_engine.py    # 30 tests

responder/
├── __init__.py
├── template.py       # {{variable}} substitution
├── engine.py         # Response building
├── test_template.py  # 18 tests
└── test_engine.py    # 17 tests

state/
├── __init__.py
├── store.py          # TTL session storage
└── test_store.py     # 25 tests

config/
├── __init__.py
├── schema.py         # Environment/file config loading
└── test_schema.py    # 17 tests

openapi/
├── __init__.py
├── loader.py         # OpenAPI spec ingestion
└── test_loader.py    # 20 tests
```

### Run Individual Modules

```bash
pytest types/              # Core domain types (15 tests)
pytest matcher/            # Matching pipeline (50 tests)
pytest responder/          # Response pipeline (35 tests)
pytest state/              # Session management (25 tests)
pytest config/             # Configuration (17 tests)
pytest openapi/            # OpenAPI loading (20 tests)
```

## Stage 2: Integration Layer (In Progress)

The glue nodes are currently being implemented:

- `core.orchestrator` — Request processing orchestrator
- `http.server` — HTTP server with routing  
- `admin.api.v2` — Admin endpoints
- `cli.runner` — CLI entry point
- `system.init` — System bootstrap

When complete, run all tests:

```bash
pytest                        # Stage 1 + Stage 2
```

## Expected Output

```
============================= test session starts ==============================
platform linux -- Python 3.x, pytest-8.x
configfile: pytest.ini
testpaths: types, matcher, responder, state, config, openapi
collected 47 items

types/test_core.py .............                                           [ 21%]
matcher/test_parser.py ....................                                [ 63%]
matcher/test_engine.py ..............................                      [100%]
responder/test_template.py .................                               [ 70%]
responder/test_engine.py .................                                 [100%]
state/test_store.py .........................                              [100%]
config/test_schema.py ................                                     [100%]
openapi/test_loader.py ....................                                [100%]

============================== 47 passed in 0.12s =============================
```

## Quick Smoke Test

For a fast sanity check without running the full test suite:

```bash
python test_all.py
```

This imports all 8 modules and verifies the contract guarantees are met.

## Troubleshooting

**Import errors?** Ensure you're running from the `content/` directory:
```bash
cd testing/session/content
pytest
```

**Module not found?** The tests use:
- Relative imports within packages (e.g., `from .core import Request`)
- `importlib` for cross-package imports to avoid `types` stdlib conflict

**Test discovery issues?**
```bash
pytest --collect-only          # See what tests pytest finds
pytest types/test_core.py -v   # Run single file
```


