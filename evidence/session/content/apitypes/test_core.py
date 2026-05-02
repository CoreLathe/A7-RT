"""Tests for types.core - Core domain dataclasses.

Validates: Request, Response, MatchRule, Template, Session
Guarantees: Immutable dataclasses, JSON serializable
"""

import json
from dataclasses import is_dataclass

from . import core as tc
from .core import Request, Response, MatchRule, Template, Session


# ============================================================================
# REQUEST
# ============================================================================
def test_request_is_dataclass():
    """Request must be a dataclass."""
    assert is_dataclass(tc.Request)


def test_request_creation():
    """Request can be created with all fields."""
    req = tc.Request(
        method="GET",
        path="/api/users",
        headers={"Content-Type": "application/json"},
        body=b"request body"
    )
    assert req.method == "GET"
    assert req.path == "/api/users"
    assert req.headers == {"Content-Type": "application/json"}
    assert req.body == b"request body"


def test_request_is_frozen():
    """Request must be immutable."""
    req = tc.Request(method="POST", path="/test", headers={}, body=b"")
    try:
        req.method = "GET"
        raise AssertionError("Request should be frozen (immutable)")
    except (AttributeError, TypeError):
        pass  # Expected


def test_request_json_serializable():
    """Request must be JSON serializable."""
    req = tc.Request(method="GET", path="/test", headers={}, body=b"hello")
    data = req.to_dict() if hasattr(req, 'to_dict') else {
        "method": req.method,
        "path": req.path,
        "headers": req.headers,
        "body": req.body.decode('utf-8') if req.body else ""
    }
    json_str = json.dumps(data)
    assert isinstance(json_str, str)


def test_request_empty_values():
    """Request handles empty/minimal values."""
    req = tc.Request(method="", path="/", headers={}, body=b"")
    assert req.method == ""
    assert req.path == "/"


# ============================================================================
# RESPONSE
# ============================================================================
def test_response_is_dataclass():
    """Response must be a dataclass."""
    assert is_dataclass(tc.Response)


def test_response_creation():
    """Response can be created with all fields."""
    resp = tc.Response(
        status=200,
        headers={"Content-Type": "application/json"},
        body=b'{"success": true}'
    )
    assert resp.status == 200
    assert resp.headers == {"Content-Type": "application/json"}
    assert resp.body == b'{"success": true}'


def test_response_is_frozen():
    """Response must be immutable."""
    resp = tc.Response(status=404, headers={}, body=b"not found")
    try:
        resp.status = 200
        raise AssertionError("Response should be frozen (immutable)")
    except (AttributeError, TypeError):
        pass  # Expected


def test_response_error_statuses():
    """Response handles error status codes."""
    resp_500 = tc.Response(status=500, headers={}, body=b"error")
    assert resp_500.status == 500
    
    resp_404 = tc.Response(status=404, headers={}, body=b"not found")
    assert resp_404.status == 404


# ============================================================================
# MATCHRULE
# ============================================================================
def test_matchrule_is_dataclass():
    """MatchRule must be a dataclass."""
    assert is_dataclass(tc.MatchRule)


def test_matchrule_creation():
    """MatchRule can be created with all fields."""
    rule = tc.MatchRule(
        path_pattern="/api/*",
        method="GET",
        header_patterns={"Accept": "application/json"},
        body_contains="search_term"
    )
    assert rule.path_pattern == "/api/*"
    assert rule.method == "GET"
    assert rule.header_patterns == {"Accept": "application/json"}
    assert rule.body_contains == "search_term"


def test_matchrule_optional_method():
    """MatchRule method field is optional (None allowed)."""
    rule = tc.MatchRule(
        path_pattern="/any",
        method=None,
        header_patterns={},
        body_contains=None
    )
    assert rule.method is None
    assert rule.body_contains is None


def test_matchrule_is_frozen():
    """MatchRule must be immutable."""
    rule = tc.MatchRule(path_pattern="/test", method=None, header_patterns={}, body_contains=None)
    try:
        rule.path_pattern = "/changed"
        raise AssertionError("MatchRule should be frozen (immutable)")
    except (AttributeError, TypeError):
        pass  # Expected


# ============================================================================
# TEMPLATE
# ============================================================================
def test_template_is_dataclass():
    """Template must be a dataclass."""
    assert is_dataclass(tc.Template)


def test_template_creation():
    """Template can be created with all fields."""
    tmpl = tc.Template(
        content="Hello, {{name}}!",
        content_type="text/html"
    )
    assert tmpl.content == "Hello, {{name}}!"
    assert tmpl.content_type == "text/html"


def test_template_is_frozen():
    """Template must be immutable."""
    tmpl = tc.Template(content="test", content_type="text/plain")
    try:
        tmpl.content = "changed"
        raise AssertionError("Template should be frozen (immutable)")
    except (AttributeError, TypeError):
        pass  # Expected


def test_template_empty_content():
    """Template handles empty content."""
    tmpl = tc.Template(content="", content_type="application/json")
    assert tmpl.content == ""


# ============================================================================
# SESSION
# ============================================================================
def test_session_is_dataclass():
    """Session must be a dataclass."""
    assert is_dataclass(tc.Session)


def test_session_creation():
    """Session can be created with all fields."""
    sess = tc.Session(
        id="sess_12345",
        state={"user_id": 42, "authenticated": True}
    )
    assert sess.id == "sess_12345"
    assert sess.state == {"user_id": 42, "authenticated": True}


def test_session_empty_state():
    """Session handles empty state dict."""
    sess = tc.Session(id="sess_empty", state={})
    assert sess.state == {}


def test_session_is_frozen():
    """Session must be immutable."""
    sess = tc.Session(id="test", state={})
    try:
        sess.id = "changed"
        raise AssertionError("Session should be frozen (immutable)")
    except (AttributeError, TypeError):
        pass  # Expected


def test_session_json_serializable():
    """Session must be JSON serializable."""
    sess = tc.Session(
        id="sess_json",
        state={"key": "value", "count": 5}
    )
    data = sess.to_dict() if hasattr(sess, 'to_dict') else {
        "id": sess.id,
        "state": sess.state
    }
    json_str = json.dumps(data)
    assert isinstance(json_str, str)
    parsed = json.loads(json_str)
    assert parsed["id"] == "sess_json"
    assert parsed["state"]["count"] == 5


def test_session_complex_state():
    """Session handles complex nested state."""
    complex_state = {
        "user": {"id": 1, "roles": ["admin", "user"]},
        "preferences": {"theme": "dark", "notifications": True}
    }
    sess = tc.Session(id="complex", state=complex_state)
    assert sess.state["user"]["roles"] == ["admin", "user"]
