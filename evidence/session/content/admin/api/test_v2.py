"""Tests for admin.api.v2 - Admin HTTP endpoints for runtime rule inspection and modification."""

import json
import sys
from pathlib import Path

# Setup imports
from apitypes.core import Request, Response, MatchRule, Template
from matcher.engine import MatcherEngine
from responder.engine import ResponderEngine
from state.store import StateStore, create_session, get_session, delete_session
from admin.api.v2 import create_admin_handler, ADMIN_PREFIX


class TestAdminPrefix:
    """Test the ADMIN_PREFIX constant."""
    
    def test_admin_prefix_is_string(self):
        """ADMIN_PREFIX should be a string."""
        assert isinstance(ADMIN_PREFIX, str)
    
    def test_admin_prefix_value(self):
        """ADMIN_PREFIX should be '/__admin'."""
        assert ADMIN_PREFIX == "/__admin"


class TestCreateAdminHandler:
    """Test the create_admin_handler factory function."""
    
    def test_returns_callable(self):
        """Should return a callable handler function."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        
        handler = create_admin_handler(matcher, responder, store)
        
        assert callable(handler)
    
    def test_non_admin_path_returns_none(self):
        """Non-admin paths should return None."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        request = Request(method="GET", path="/api/users")
        result = handler(request)
        
        assert result is None
    
    def test_admin_prefix_check_uses_startswith(self):
        """Handler should use startswith to check admin prefix."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        # Path that contains but doesn't start with /__admin
        request = Request(method="GET", path="/api/__admin/hack")
        result = handler(request)
        
        assert result is None


class TestGetRules:
    """Test GET /__admin/rules endpoint."""
    
    def test_get_rules_returns_json_list(self):
        """GET /__admin/rules should return JSON list of rules."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        # Register some rules
        rule1 = MatchRule(path_pattern="/api/*", method="GET")
        rule2 = MatchRule(path_pattern="/admin/*", method="POST")
        matcher.register_rule(rule1, priority=10)
        matcher.register_rule(rule2, priority=5)
        
        request = Request(method="GET", path="/__admin/rules")
        response = handler(request)
        
        assert response is not None
        assert response.status == 200
        assert response.headers.get("Content-Type") == "application/json"
        
        # Parse response body
        body_str = response.body.decode("utf-8")
        rules_data = json.loads(body_str)
        
        assert len(rules_data) == 2
        assert rules_data[0]["path_pattern"] == "/api/*"
        assert rules_data[1]["path_pattern"] == "/admin/*"
    
    def test_get_rules_empty_list(self):
        """GET /__admin/rules with no rules returns empty list."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        request = Request(method="GET", path="/__admin/rules")
        response = handler(request)
        
        assert response is not None
        assert response.status == 200
        
        body_str = response.body.decode("utf-8")
        rules_data = json.loads(body_str)
        
        assert rules_data == []


class TestPostRules:
    """Test POST /__admin/rules endpoint."""
    
    def test_post_rule_creates_and_registers(self):
        """POST /__admin/rules should create MatchRule and register via matcher."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        rule_data = {
            "path_pattern": "/new/*",
            "method": "GET",
            "header_patterns": {"X-Auth": "token-*"},
            "body_contains": "test",
            "priority": 10
        }
        
        request = Request(
            method="POST",
            path="/__admin/rules",
            body=json.dumps(rule_data).encode("utf-8")
        )
        response = handler(request)
        
        assert response is not None
        assert response.status == 201
        
        # Verify rule was registered
        rules = matcher.get_rules()
        assert len(rules) == 1
        assert rules[0].path_pattern == "/new/*"
        assert rules[0].method == "GET"
    
    def test_post_rule_minimal_data(self):
        """POST with minimal data should work."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        rule_data = {"path_pattern": "/simple"}
        
        request = Request(
            method="POST",
            path="/__admin/rules",
            body=json.dumps(rule_data).encode("utf-8")
        )
        response = handler(request)
        
        assert response is not None
        assert response.status == 201
        
        rules = matcher.get_rules()
        assert len(rules) == 1
        assert rules[0].path_pattern == "/simple"
        assert rules[0].method is None
    
    def test_post_invalid_json_returns_400(self):
        """POST with invalid JSON should return 400."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        request = Request(
            method="POST",
            path="/__admin/rules",
            body=b"not valid json"
        )
        response = handler(request)
        
        assert response is not None
        assert response.status == 400


class TestDeleteSession:
    """Test DELETE /__admin/sessions/{id} endpoint."""
    
    def test_delete_session_clears_via_store(self):
        """DELETE should clear session via store.delete_session()."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        # Create a session first
        session = create_session(store, ttl_seconds=3600)
        session_id = session.id
        
        # Verify session exists
        assert get_session(store, session_id) is not None
        
        # Delete via admin endpoint
        request = Request(method="DELETE", path=f"/__admin/sessions/{session_id}")
        response = handler(request)
        
        assert response is not None
        assert response.status == 200
        
        # Verify session was deleted
        assert get_session(store, session_id) is None
    
    def test_delete_nonexistent_session_succeeds(self):
        """DELETE for nonexistent session should still succeed."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        request = Request(method="DELETE", path="/__admin/sessions/nonexistent-id")
        response = handler(request)
        
        assert response is not None
        assert response.status == 200


class TestNonAdminPaths:
    """Test that non-admin paths return None for fallback handling."""
    
    def test_regular_api_paths_return_none(self):
        """Regular API paths should return None."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        paths = ["/", "/api", "/api/users", "/health", "/__adminfake"]
        
        for path in paths:
            request = Request(method="GET", path=path)
            result = handler(request)
            assert result is None, f"Path {path} should return None"


class TestUnmatchedAdminPaths:
    """Test unmatched admin paths return 404."""
    
    def test_unknown_admin_path_returns_404(self):
        """Unknown admin paths should return 404."""
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        handler = create_admin_handler(matcher, responder, store)
        
        request = Request(method="GET", path="/__admin/unknown")
        response = handler(request)
        
        assert response is not None
        assert response.status == 404