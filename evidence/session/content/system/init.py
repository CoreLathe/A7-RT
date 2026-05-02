"""System bootstrap: wire components, load initial rules, start server."""

import atexit
import signal
import sys
from dataclasses import dataclass
from typing import Optional

from config import Config
from apitypes import Request, Response, MatchRule, Template
from matcher import MatcherEngine
from responder import ResponderEngine, register_template, build_response
from state import StateStore, create_session, get_session, update_session
from http import HTTPServer, create_server
from openapi import load_from_spec, OpenAPIError
from admin.api import create_admin_handler, ADMIN_PREFIX


@dataclass
class SystemContext:
    """Container for all system components."""
    matcher: MatcherEngine
    responder: ResponderEngine
    state: StateStore
    server: HTTPServer


def initialize_system(config: Config, spec_path: str | None) -> SystemContext:
    """
    Initialize the system with all components wired together.
    
    Args:
        config: Server configuration
        spec_path: Path to OpenAPI spec file, or None to skip spec loading
        
    Returns:
        SystemContext containing all initialized components
        
    Raises:
        RuntimeError: On initialization failure
    """
    try:
        # Create state store
        state_store = StateStore()
        
        # Create matcher engine
        matcher_engine = MatcherEngine()
        
        # Create responder engine
        responder_engine = ResponderEngine()
        
        # Load OpenAPI spec and register rules if provided
        if spec_path is not None:
            try:
                rules_and_templates = load_from_spec(spec_path)
                for match_rule, template in rules_and_templates:
                    # Register rule with default priority 0
                    matcher_engine.register_rule(match_rule, priority=0)
                    # Register template using path_pattern as rule_id
                    register_template(responder_engine, match_rule.path_pattern, template)
            except OpenAPIError as e:
                raise RuntimeError(f"Failed to load OpenAPI spec: {e}")
            except Exception as e:
                raise RuntimeError(f"Failed to register OpenAPI rules: {e}")
        
        # Create admin handler
        admin_handler = create_admin_handler(
            matcher=matcher_engine,
            responder=responder_engine,
            store=state_store
        )
        
        # Create main request handler
        def request_handler(request: Request) -> Response:
            # Try admin handler first
            admin_response = admin_handler(request)
            if admin_response is not None:
                return admin_response
            
            # Regular request handling
            # Get or create session (using a simple session ID from headers or create new)
            session_id = request.headers.get("X-Session-ID")
            if session_id:
                session = get_session(state_store, session_id)
                if session is None:
                    # Create new session if expired/missing
                    session = create_session(state_store, config.ttl_default)
            else:
                session = create_session(state_store, config.ttl_default)
            
            # Match request against rules
            match_rule = matcher_engine.match_request(request)
            if match_rule is None:
                return Response(
                    status=404,
                    headers={"Content-Type": "application/json"},
                    body=b'{"error": "No matching rule found"}'
                )
            
            # Build response using responder
            try:
                response = build_response(responder_engine, match_rule, request, session)
                # Update session state
                update_session(state_store, session)
                return response
            except KeyError:
                return Response(
                    status=500,
                    headers={"Content-Type": "application/json"},
                    body=b'{"error": "Template not found for rule"}'
                )
        
        # Create HTTP server
        server = create_server(config, request_handler)
        
        # Register cleanup handlers for graceful shutdown
        def cleanup():
            """Cleanup function for graceful shutdown."""
            if server.is_running:
                server.shutdown()
        
        # Register atexit handler
        atexit.register(cleanup)
        
        # Register signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            """Handle shutdown signals."""
            cleanup()
            sys.exit(0)
        
        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
        except (ValueError, AttributeError):
            # Signal handling may not be available in some contexts
            pass
        
        # Create and return system context
        return SystemContext(
            matcher=matcher_engine,
            responder=responder_engine,
            state=state_store,
            server=server
        )
        
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"System initialization failed: {e}")
