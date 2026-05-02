"""Response builder: evaluates templates, applies session state, generates Response objects."""

import importlib.util
import sys
from pathlib import Path
from typing import Any

# Import types.core using importlib (guard against re-loading)
if "types_core" not in sys.modules:
    _types_path = Path(__file__).parent.parent / "apitypes" / "core.py"
    spec_types = importlib.util.spec_from_file_location("types_core", _types_path)
    if spec_types is None or spec_types.loader is None:
        raise ImportError("Could not load apitypes/core.py")
    types_core = importlib.util.module_from_spec(spec_types)
    sys.modules["types_core"] = types_core
    spec_types.loader.exec_module(types_core)
types_core = sys.modules["types_core"]

Request = types_core.Request
Response = types_core.Response
MatchRule = types_core.MatchRule
Template = types_core.Template
Session = types_core.Session

# Import responder.template using importlib
_template_path = Path(__file__).parent / "template.py"
spec_template = importlib.util.spec_from_file_location("responder_template", _template_path)
if spec_template is None or spec_template.loader is None:
    raise ImportError("Could not load responder/template.py")
responder_template = importlib.util.module_from_spec(spec_template)
sys.modules["responder_template"] = responder_template
spec_template.loader.exec_module(responder_template)

evaluate_template = responder_template.evaluate_template


class ResponderEngine:
    """Engine for building responses from templates with session state substitution."""

    def __init__(self) -> None:
        """Initialize the responder engine with an empty template registry."""
        self._templates: dict[str, Template] = {}


def register_template(engine: ResponderEngine, rule_id: str, template: Template) -> None:
    """
    Register a template for a given rule ID.

    Args:
        engine: The ResponderEngine instance to register the template with
        rule_id: The identifier for the rule (typically match_rule.path_pattern)
        template: The Template to associate with this rule
    """
    engine._templates[rule_id] = template


def build_response(
    engine: ResponderEngine, match_rule: MatchRule, request: Request, session: Session
) -> Response:
    """
    Build a response by evaluating a template with session state.

    Args:
        engine: The ResponderEngine containing registered templates
        match_rule: The rule that matched the request (used to look up template)
        request: The incoming request (available for template context)
        session: The user session containing state variables for template substitution

    Returns:
        A Response with status 200, Content-Type header from template, and UTF-8 encoded body

    Raises:
        KeyError: If no template is registered for the rule's path_pattern
    """
    # Look up template using match_rule.path_pattern as rule_id
    rule_id = match_rule.path_pattern
    if rule_id not in engine._templates:
        raise KeyError(f"No template registered for rule: {rule_id}")

    template = engine._templates[rule_id]

    # Evaluate template with session state as context
    context: dict[str, Any] = dict(session.state)
    evaluated_content = evaluate_template(template.content, context)

    # Build response with UTF-8 encoded body and Content-Type header
    body_bytes = evaluated_content.encode("utf-8")
    headers = {"Content-Type": template.content_type}

    return Response(status=200, headers=headers, body=body_bytes)
