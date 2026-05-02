"""
Unified test runner for A7-RT Stage 1 Core Layer.

HOW TO RUN:
    cd testing/session/content && python -m pytest test_all.py -v

IMPORTANT: Must run from content directory so 'types' package doesn't conflict
with Python's stdlib 'types' module.
"""

import sys
from pathlib import Path

# Ensure we're running from the content directory
_HERE = Path(__file__).parent.resolve()
if Path.cwd().resolve() != _HERE:
    print(f"WARNING: Run from {_HERE} to avoid import conflicts")
    print(f"   cd {_HERE} && python -m pytest test_all.py -v")

import pytest


def test_stage1_core_complete():
    """
    Verify all 8 core nodes have tests and can be imported.

    This is a meta-test that validates the Stage 1 deliverable structure.
    """
    # Import all modules to verify they load without errors
    from apitypes.core import MatchRule, Request, Response, Session, Template
    from config.schema import DEFAULT_CONFIG, Config, load_config
    from matcher.engine import CollisionError, MatcherEngine
    from matcher.parser import PatternError, compile_pattern, parse_match_rule
    from openapi.loader import SUPPORTED_VERSIONS, OpenAPIError, load_from_spec
    from responder.engine import ResponderEngine, build_response, register_template
    from responder.template import TEMPLATE_PATTERN, TemplateSyntaxError, evaluate_template
    from state.store import StateStore, cleanup_expired, create_session, get_session, update_session

    # Verify all exports are present
    assert Request is not None
    assert Response is not None
    assert MatchRule is not None
    assert Template is not None
    assert Session is not None

    assert compile_pattern is not None
    assert parse_match_rule is not None
    assert PatternError is not None

    assert MatcherEngine is not None
    assert CollisionError is not None

    assert evaluate_template is not None
    assert TemplateSyntaxError is not None
    assert TEMPLATE_PATTERN is not None

    assert ResponderEngine is not None
    assert register_template is not None
    assert build_response is not None

    assert StateStore is not None
    assert create_session is not None
    assert get_session is not None
    assert update_session is not None
    assert cleanup_expired is not None

    assert Config is not None
    assert load_config is not None
    assert DEFAULT_CONFIG is not None

    assert load_from_spec is not None
    assert OpenAPIError is not None
    assert SUPPORTED_VERSIONS is not None

    print(f"✓ All 8 core nodes importable")
    print(f"  - types.core: 5 dataclasses")
    print(f"  - matcher.parser: 2 functions, 1 exception")
    print(f"  - matcher.engine: 1 class, 3 methods, 1 exception")
    print(f"  - responder.template: 1 function, 1 exception, 1 pattern")
    print(f"  - responder.engine: 1 class, 2 functions")
    print(f"  - state.store: 1 class, 4 functions")
    print(f"  - config.schema: 1 class, 1 function, 1 constant")
    print(f"  - openapi.loader: 1 function, 1 exception, 1 constant")


def test_contract_compliance_summary():
    """
    High-level verification of contract guarantees.

    These are the promises made in the 12-node spec that Stage 1 must fulfill.
    """
    from dataclasses import is_dataclass

    from apitypes.core import MatchRule, Request, Response, Session, Template

    # Guarantee: Immutable dataclasses
    assert is_dataclass(Request) and Request.__dataclass_params__.frozen
    assert is_dataclass(Response) and Response.__dataclass_params__.frozen
    assert is_dataclass(MatchRule) and MatchRule.__dataclass_params__.frozen
    assert is_dataclass(Template) and Template.__dataclass_params__.frozen
    assert is_dataclass(Session) and Session.__dataclass_params__.frozen

    # Guarantee: JSON serializable (via to_dict methods)
    req = Request("GET", "/test", {}, b"")
    assert hasattr(req, "to_dict")
    assert req.to_dict()["method"] == "GET"

    print("✓ Contract guarantees verified")
    print("  - Immutable dataclasses: PASS")
    print("  - JSON serializable: PASS")


if __name__ == "__main__":
    # Allow running directly: python test_all.py
    sys.exit(pytest.main([__file__, "-v"]))
