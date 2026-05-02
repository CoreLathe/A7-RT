"""Tests for responder.engine - Response builder with template evaluation."""

import sys
import importlib.util
from pathlib import Path

# Load types from apitypes.core (reuse cached module if already loaded)
if "types_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("types_core", Path(__file__).parent.parent / "apitypes" / "core.py")
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
_types_core = sys.modules["types_core"]
Request = _types_core.Request
Response = _types_core.Response
MatchRule = _types_core.MatchRule
Template = _types_core.Template
Session = _types_core.Session

# Import from responder.engine (the module being tested)
spec_engine = importlib.util.spec_from_file_location("responder_engine", Path(__file__).parent / "engine.py")
responder_engine = importlib.util.module_from_spec(spec_engine)
sys.modules["responder_engine"] = responder_engine
spec_engine.loader.exec_module(responder_engine)

ResponderEngine = responder_engine.ResponderEngine
build_response = responder_engine.build_response
register_template = responder_engine.register_template


def test_responder_engine_instantiation():
    """ResponderEngine can be created."""
    engine = ResponderEngine()
    assert engine is not None


def test_register_template_stores_template():
    """register_template associates template with rule_id."""
    engine = ResponderEngine()
    template = Template(content="Hello World", content_type="text/plain")
    register_template(engine, "rule_1", template)
    # Template should be stored - we'll verify via build_response


def test_build_response_basic():
    """build_response creates Response from template."""
    engine = ResponderEngine()
    template = Template(content="Hello World", content_type="text/plain")
    register_template(engine, "/api/hello", template)
    
    request = Request(method="GET", path="/api/hello")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/hello")
    
    response = build_response(engine, rule, request, session)
    
    assert isinstance(response, Response)
    assert response.status == 200
    assert response.body == b"Hello World"
    assert response.headers.get("Content-Type") == "text/plain"


def test_build_response_with_template_variables():
    """build_response evaluates {{variable}} placeholders."""
    engine = ResponderEngine()
    template = Template(content="Hello {{name}}!", content_type="text/plain")
    register_template(engine, "/api/greet", template)
    
    request = Request(method="GET", path="/api/greet")
    session = Session(id="sess_123", state={"name": "Alice"})
    rule = MatchRule(path_pattern="/api/greet")
    
    response = build_response(engine, rule, request, session)
    
    assert response.body == b"Hello Alice!"


def test_build_response_session_state_variables():
    """Template variables come from session state."""
    engine = ResponderEngine()
    template = Template(content="User: {{username}}, Role: {{role}}", content_type="text/plain")
    register_template(engine, "/api/user", template)
    
    request = Request(method="GET", path="/api/user")
    session = Session(id="sess_123", state={"username": "bob", "role": "admin"})
    rule = MatchRule(path_pattern="/api/user")
    
    response = build_response(engine, rule, request, session)
    
    assert b"User: bob" in response.body
    assert b"Role: admin" in response.body


def test_build_response_missing_variable_renders_empty():
    """Missing template variables render as empty string."""
    engine = ResponderEngine()
    template = Template(content="Hello {{missing}}!", content_type="text/plain")
    register_template(engine, "/api/test", template)
    
    request = Request(method="GET", path="/api/test")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/test")
    
    response = build_response(engine, rule, request, session)
    
    assert response.body == b"Hello !"


def test_build_response_keyerror_on_missing_template():
    """KeyError raised when no template registered for rule."""
    engine = ResponderEngine()
    
    request = Request(method="GET", path="/api/unknown")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/unknown")
    
    try:
        build_response(engine, rule, request, session)
        assert False, "Expected KeyError"
    except KeyError:
        pass  # Expected


def test_build_response_uses_rule_path_pattern_as_key():
    """build_response uses rule.path_pattern to look up template."""
    engine = ResponderEngine()
    template = Template(content="Specific Response", content_type="text/plain")
    register_template(engine, "/api/specific", template)
    
    request = Request(method="GET", path="/api/specific")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/specific", method="GET")
    
    response = build_response(engine, rule, request, session)
    
    assert response.body == b"Specific Response"


def test_build_response_json_content_type():
    """Content-Type header set from template content_type."""
    engine = ResponderEngine()
    template = Template(content='{"status": "ok"}', content_type="application/json")
    register_template(engine, "/api/json", template)
    
    request = Request(method="GET", path="/api/json")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/json")
    
    response = build_response(engine, rule, request, session)
    
    assert response.headers.get("Content-Type") == "application/json"
    assert response.body == b'{"status": "ok"}'


def test_build_response_html_content_type():
    """HTML templates get correct Content-Type."""
    engine = ResponderEngine()
    template = Template(content="<html><body>Hello</body></html>", content_type="text/html")
    register_template(engine, "/api/html", template)
    
    request = Request(method="GET", path="/api/html")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/html")
    
    response = build_response(engine, rule, request, session)
    
    assert response.headers.get("Content-Type") == "text/html"


def test_response_body_is_utf8_encoded():
    """Response body is UTF-8 encoded bytes."""
    engine = ResponderEngine()
    template = Template(content="Hello 世界 🌍", content_type="text/plain")
    register_template(engine, "/api/unicode", template)
    
    request = Request(method="GET", path="/api/unicode")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/unicode")
    
    response = build_response(engine, rule, request, session)
    
    # Verify it's bytes and correctly encoded
    assert isinstance(response.body, bytes)
    assert response.body == "Hello 世界 🌍".encode("utf-8")
    # Verify we can decode it back
    assert response.body.decode("utf-8") == "Hello 世界 🌍"


def test_multiple_templates_registered():
    """Can register multiple templates for different rules."""
    engine = ResponderEngine()
    
    template1 = Template(content="Response 1", content_type="text/plain")
    template2 = Template(content="Response 2", content_type="application/json")
    
    register_template(engine, "/api/one", template1)
    register_template(engine, "/api/two", template2)
    
    request = Request(method="GET", path="/api/one")
    session = Session(id="sess_123", state={})
    rule1 = MatchRule(path_pattern="/api/one")
    rule2 = MatchRule(path_pattern="/api/two")
    
    response1 = build_response(engine, rule1, request, session)
    request2 = Request(method="GET", path="/api/two")
    response2 = build_response(engine, rule2, request2, session)
    
    assert response1.body == b"Response 1"
    assert response1.headers.get("Content-Type") == "text/plain"
    assert response2.body == b"Response 2"
    assert response2.headers.get("Content-Type") == "application/json"


def test_register_template_overwrites_existing():
    """Registering template with same rule_id overwrites previous."""
    engine = ResponderEngine()
    
    template1 = Template(content="Old", content_type="text/plain")
    template2 = Template(content="New", content_type="text/plain")
    
    register_template(engine, "/api/update", template1)
    register_template(engine, "/api/update", template2)
    
    request = Request(method="GET", path="/api/update")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/update")
    
    response = build_response(engine, rule, request, session)
    
    assert response.body == b"New"


def test_build_response_default_status_200():
    """Response defaults to status 200."""
    engine = ResponderEngine()
    template = Template(content="OK", content_type="text/plain")
    register_template(engine, "/api/ok", template)
    
    request = Request(method="GET", path="/api/ok")
    session = Session(id="sess_123", state={})
    rule = MatchRule(path_pattern="/api/ok")
    
    response = build_response(engine, rule, request, session)
    
    assert response.status == 200


def test_template_with_complex_session_state():
    """Template variables work with nested session state values."""
    engine = ResponderEngine()
    template = Template(content="Count: {{count}}", content_type="text/plain")
    register_template(engine, "/api/count", template)
    
    request = Request(method="GET", path="/api/count")
    session = Session(id="sess_123", state={"count": 42})
    rule = MatchRule(path_pattern="/api/count")
    
    response = build_response(engine, rule, request, session)
    
    assert b"42" in response.body
