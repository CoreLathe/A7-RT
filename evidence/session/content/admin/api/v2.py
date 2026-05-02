"""Admin HTTP endpoints for runtime rule inspection and modification."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Load types from apitypes.core (guard against re-loading)
if "types_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "types_core", Path(__file__).parent.parent.parent / "apitypes" / "core.py"
    )
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
Request = sys.modules["types_core"].Request
Response = sys.modules["types_core"].Response
MatchRule = sys.modules["types_core"].MatchRule


ADMIN_PREFIX: str = "/__admin"


def create_admin_handler(
    matcher: Any, responder: Any, store: Any
) -> Callable[[Request], Optional[Response]]:
    """Create an admin handler that routes /__admin/* paths to admin operations.

    Args:
        matcher: Object with register_rule() and get_rules() methods
        responder: Object with register_template() method
        store: Object with get_session() and delete_session() methods

    Returns:
        Handler function that processes admin requests or returns None for non-admin paths
    """

    def handler(request: Request) -> Optional[Response]:
        # Check if path starts with admin prefix
        if not request.path.startswith(ADMIN_PREFIX):
            return None

        # Route to appropriate handler based on path and method
        if request.path == "/__admin/rules":
            if request.method == "GET":
                return _handle_get_rules(matcher)
            elif request.method == "POST":
                return _handle_post_rules(matcher, request)

        # Handle DELETE /__admin/sessions/{id}
        if request.method == "DELETE" and request.path.startswith("/__admin/sessions/"):
            session_id = request.path[len("/__admin/sessions/") :]
            if session_id:
                return _handle_delete_session(store, session_id)

        # Return 404 for unmatched admin paths
        return Response(
            status=404,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": "Not found"}).encode("utf-8"),
        )

    return handler


def _handle_get_rules(matcher: Any) -> Response:
    """Handle GET /__admin/rules - return list of registered matcher rules."""
    rules = matcher.get_rules()
    rules_data = [rule.to_dict() for rule in rules]

    return Response(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(rules_data).encode("utf-8"),
    )


def _handle_post_rules(matcher: Any, request: Request) -> Response:
    """Handle POST /__admin/rules - create and register a new MatchRule."""
    try:
        # Parse JSON body
        body_str = request.body.decode("utf-8", errors="replace") if request.body else "{}"
        data = json.loads(body_str)

        # Create MatchRule from JSON data
        rule = MatchRule(
            path_pattern=data.get("path_pattern", ""),
            method=data.get("method"),
            header_patterns=data.get("header_patterns", {}),
            body_contains=data.get("body_contains"),
        )

        # Register via matcher
        priority = data.get("priority", 0)
        matcher.register_rule(rule, priority)

        return Response(
            status=201,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"status": "created", "rule": rule.to_dict()}).encode("utf-8"),
        )
    except json.JSONDecodeError as e:
        return Response(
            status=400,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": f"Invalid JSON: {str(e)}"}).encode("utf-8"),
        )
    except Exception as e:
        return Response(
            status=400,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": str(e)}).encode("utf-8"),
        )


def _handle_delete_session(store: Any, session_id: str) -> Response:
    """Handle DELETE /__admin/sessions/{id} - clear session via store."""
    store.delete_session(session_id)

    return Response(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps({"status": "deleted", "session_id": session_id}).encode("utf-8"),
    )
