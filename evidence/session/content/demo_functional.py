"""Functional demonstration of the API mock server components."""

import sys
import time
import threading
from pathlib import Path

# Add content dir to path
sys.path.insert(0, str(Path(__file__).parent))

from apitypes import Request, Response, MatchRule, Template
from matcher import MatcherEngine
from responder import ResponderEngine, register_template, build_response
from state import StateStore, create_session, get_session
from config.schema import load_config, DEFAULT_CONFIG


def demo_matcher():
    """Demo: Pattern matching engine."""
    print("=" * 60)
    print("DEMO 1: Pattern Matching Engine")
    print("=" * 60)
    
    matcher = MatcherEngine()
    
    # Register some rules
    rule1 = MatchRule(path_pattern="/api/users/*", method="GET")
    rule2 = MatchRule(path_pattern="/api/items/*", method="GET")
    
    matcher.register_rule(rule1, priority=1)
    matcher.register_rule(rule2, priority=2)
    
    # Create requests
    req1 = Request(method="GET", path="/api/users/123", headers={}, body=b"")
    req2 = Request(method="POST", path="/api/users/123", headers={}, body=b"")
    req3 = Request(method="GET", path="/api/items/456", headers={}, body=b"")
    req4 = Request(method="GET", path="/api/other", headers={}, body=b"")
    
    print(f"Request 1: GET /api/users/123")
    print(f"  Match: {matcher.match_request(req1)}")
    
    print(f"Request 2: POST /api/users/123")
    print(f"  Match: {matcher.match_request(req2)}")
    
    print(f"Request 3: GET /api/items/456")
    print(f"  Match: {matcher.match_request(req3)}")
    
    print(f"Request 4: GET /api/other")
    print(f"  Match: {matcher.match_request(req4)}")
    
    print()


def demo_templating():
    """Demo: Template engine."""
    print("=" * 60)
    print("DEMO 2: Template Engine")
    print("=" * 60)
    
    from responder.template import evaluate_template
    
    template = "Hello {{name}}, your id is {{user_id}}!"
    context = {"name": "Alice", "user_id": 42}
    
    result = evaluate_template(template, context)
    print(f"Template: {template}")
    print(f"Context:  {context}")
    print(f"Result:   {result}")
    
    # Missing variable
    context2 = {"name": "Bob"}
    result2 = evaluate_template(template, context2)
    print(f"\nWith missing 'user_id': {result2}")
    print()


def demo_response_builder():
    """Demo: Response building."""
    print("=" * 60)
    print("DEMO 3: Response Builder")
    print("=" * 60)
    
    matcher = MatcherEngine()
    responder = ResponderEngine()
    store = StateStore()
    
    # Setup rule and template
    rule = MatchRule(path_pattern="/api/user", method="GET")
    template = Template(content='{"id": {{id}}, "name": "{{name}}"}', content_type="application/json")
    
    matcher.register_rule(rule, priority=1)
    register_template(responder, rule.path_pattern, template)
    
    # Create session
    session = create_session(store, ttl_seconds=300)
    session.state["id"] = 42
    session.state["name"] = "TestUser"
    
    # Build request and get response
    request = Request(method="GET", path="/api/user", headers={}, body=b"")
    matched_rule = matcher.match_request(request)
    response = build_response(responder, matched_rule, request, session)
    
    print(f"Request:  {request.method} {request.path}")
    print(f"Response: Status {response.status}")
    print(f"Headers:  {dict(response.headers)}")
    print(f"Body:     {response.body.decode()}")
    print()


def demo_session_management():
    """Demo: Session store with TTL."""
    print("=" * 60)
    print("DEMO 4: Session Management")
    print("=" * 60)
    
    store = StateStore()
    
    # Create sessions
    session1 = create_session(store, ttl_seconds=300)
    session1.state["data"] = "session 1 data"
    
    session2 = create_session(store, ttl_seconds=300)
    session2.state["data"] = "session 2 data"
    
    print(f"Created session 1: {session1.id[:8]}...")
    print(f"Created session 2: {session2.id[:8]}...")
    
    # Retrieve sessions
    retrieved1 = get_session(store, session1.id)
    retrieved2 = get_session(store, session2.id)
    
    print(f"\nRetrieved session 1 data: {retrieved1.state.get('data')}")
    print(f"Retrieved session 2 data: {retrieved2.state.get('data')}")
    print()


def demo_config():
    """Demo: Configuration loading."""
    print("=" * 60)
    print("DEMO 5: Configuration")
    print("=" * 60)
    
    # Default config
    config1 = load_config({}, None)
    print(f"Default config: port={config1.port}, host={config1.host}")
    
    # Override with env
    config2 = load_config({"PORT": "9090", "HOST": "0.0.0.0"}, None)
    print(f"With env override: port={config2.port}, host={config2.host}")
    print()


def main():
    print("\n" + "=" * 60)
    print("A7-RT API MOCK SERVER - FUNCTIONAL DEMO")
    print("=" * 60 + "\n")
    
    demo_config()
    demo_matcher()
    demo_templating()
    demo_response_builder()
    demo_session_management()
    
    print("=" * 60)
    print("All demos completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
