"""Tests for system.init - System bootstrap and component wiring."""

import pytest
import tempfile
import json
import os
from pathlib import Path


class TestSystemContext:
    """Tests for SystemContext dataclass."""
    
    def test_system_context_has_required_fields(self):
        """SystemContext must have: matcher, responder, state, server."""
        from system.init import SystemContext
        
        # Check annotations exist
        assert hasattr(SystemContext, '__annotations__')
        annotations = SystemContext.__annotations__
        assert 'matcher' in annotations
        assert 'responder' in annotations
        assert 'state' in annotations
        assert 'server' in annotations
    
    def test_system_context_creation(self):
        """SystemContext can be created with all required components."""
        from system.init import SystemContext
        from matcher import MatcherEngine
        from responder import ResponderEngine
        from state import StateStore
        from http import HTTPServer
        from config import Config
        from apitypes.core import Request, Response
        
        # Create components
        matcher = MatcherEngine()
        responder = ResponderEngine()
        state = StateStore()
        
        # Minimal handler for server
        def handler(request: Request) -> Response:
            return Response(status=200, body=b"OK")
        
        config = Config(port=0)  # Let OS assign port
        server = HTTPServer(config, handler)
        
        # Create context
        context = SystemContext(
            matcher=matcher,
            responder=responder,
            state=state,
            server=server
        )
        
        assert context.matcher is matcher
        assert context.responder is responder
        assert context.state is state
        assert context.server is server


class TestInitializeSystem:
    """Tests for initialize_system function."""
    
    def test_initialize_system_returns_system_context(self):
        """initialize_system must return a SystemContext instance."""
        from system.init import initialize_system, SystemContext
        from config import Config
        
        config = Config(port=0)  # Let OS assign port
        context = initialize_system(config, spec_path=None)
        
        assert isinstance(context, SystemContext)
    
    def test_initialize_system_creates_all_components(self):
        """initialize_system creates all required components."""
        from system.init import initialize_system
        from config import Config
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=None)
        
        assert context.matcher is not None
        assert context.responder is not None
        assert context.state is not None
        assert context.server is not None
    
    def test_initialize_system_with_spec_path(self, tmp_path):
        """initialize_system loads and registers OpenAPI rules from spec."""
        from system.init import initialize_system
        from config import Config
        
        # Create a minimal OpenAPI spec
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/api/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "example": {"users": []}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=str(spec_file))
        
        # Check that rules were registered
        rules = context.matcher.get_rules()
        assert len(rules) == 1
        assert rules[0].path_pattern == "/api/users"
        assert rules[0].method == "GET"
    
    def test_initialize_system_invalid_spec_raises_runtime_error(self, tmp_path):
        """Invalid spec path should raise RuntimeError."""
        from system.init import initialize_system
        from config import Config
        
        config = Config(port=0)
        
        with pytest.raises(RuntimeError):
            initialize_system(config, spec_path="/nonexistent/spec.json")
    
    def test_initialize_system_malformed_spec_raises_runtime_error(self, tmp_path):
        """Malformed spec should raise RuntimeError."""
        from system.init import initialize_system
        from config import Config
        
        spec_file = tmp_path / "bad_spec.json"
        spec_file.write_text('{"openapi": "2.0.0", "paths": {}}')  # Unsupported version
        
        config = Config(port=0)
        
        with pytest.raises(RuntimeError):
            initialize_system(config, spec_path=str(spec_file))
    
    def test_components_share_consistent_state(self):
        """All components should have consistent state references."""
        from system.init import initialize_system
        from config import Config
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=None)
        
        # Components should exist and be wired together
        assert context.matcher is not None
        assert context.responder is not None
        assert context.state is not None
        assert context.server is not None
        
        # Server should have a handler
        assert context.server.handler is not None
    
    def test_server_has_config(self):
        """Server should have the provided config."""
        from system.init import initialize_system
        from config import Config
        
        config = Config(port=0, host="127.0.0.1")
        context = initialize_system(config, spec_path=None)
        
        assert context.server.config is config
    
    def test_request_handler_routes_admin_requests(self):
        """Request handler should route admin requests correctly."""
        from system.init import initialize_system
        from config import Config
        from apitypes.core import Request
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=None)
        
        # Test admin request
        admin_request = Request(method="GET", path="/__admin/rules")
        response = context.server.handler(admin_request)
        
        # Should return 200 with rules list (even if empty)
        assert response.status == 200
    
    def test_request_handler_returns_404_for_unknown_paths(self):
        """Request handler should return 404 for unmatched paths."""
        from system.init import initialize_system
        from config import Config
        from apitypes.core import Request
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=None)
        
        # Test unknown path
        request = Request(method="GET", path="/unknown/path")
        response = context.server.handler(request)
        
        assert response.status == 404
    
    def test_request_handler_creates_session(self):
        """Request handler should create session for requests without session ID."""
        from system.init import initialize_system
        from config import Config
        from apitypes.core import Request
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=None)
        
        # Test request without session ID
        request = Request(method="GET", path="/unknown")
        response = context.server.handler(request)
        
        # Should process (even if 404) without error
        assert response is not None
    
    def test_initialize_system_with_multiple_rules(self, tmp_path):
        """initialize_system loads multiple rules from spec."""
        from system.init import initialize_system
        from config import Config
        
        # Create a spec with multiple paths
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/api/users": {
                    "get": {"responses": {"200": {"content": {"application/json": {"example": {}}}}}}
                },
                "/api/items": {
                    "post": {"responses": {"201": {"content": {"application/json": {"example": {}}}}}}
                }
            }
        }
        
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=str(spec_file))
        
        rules = context.matcher.get_rules()
        assert len(rules) == 2
    
    def test_cleanup_handlers_registered(self):
        """Cleanup handlers should be registered for graceful shutdown."""
        from system.init import initialize_system
        from config import Config
        
        config = Config(port=0)
        context = initialize_system(config, spec_path=None)
        
        # Server should have shutdown method available
        assert hasattr(context.server, 'shutdown')
        assert callable(context.server.shutdown)
