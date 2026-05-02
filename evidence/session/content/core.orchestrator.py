"""Request processing orchestrator: coordinates matcher, responder, and state."""

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

# Load types from apitypes.core (guard against re-loading)
if "types_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("types_core", Path(__file__).parent.parent / "apitypes" / "core.py")
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
Request = sys.modules["types_core"].Request
Response = sys.modules["types_core"].Response
MatchRule = sys.modules["types_core"].MatchRule
Session = sys.modules["types_core"].Session

# Import from matcher.engine
from matcher.engine import MatcherEngine

# Import from responder.engine
from responder.engine import ResponderEngine, build_response

# Import from state.store
from state.store import StateStore, create_session, get_session


class Orchestrator:
    """Container for matcher, responder, and store components.
    
    The orchestrator wires together the request matching, session management,
    and response generation components to process HTTP requests.
    """
    
    def __init__(
        self,
        matcher: MatcherEngine,
        responder: ResponderEngine,
        store: StateStore
    ) -> None:
        """Initialize the orchestrator with required components.
        
        Args:
            matcher: Engine for matching requests against rules
            responder: Engine for building responses from templates
            store: Store for session state management
        """
        self.matcher = matcher
        self.responder = responder
        self.store = store


def create_orchestrator(
    matcher: MatcherEngine,
    responder: ResponderEngine,
    store: StateStore
) -> Orchestrator:
    """Factory that wires components into an Orchestrator.
    
    Args:
        matcher: Engine for matching requests against rules
        responder: Engine for building responses from templates
        store: Store for session state management
        
    Returns:
        Configured Orchestrator instance ready to handle requests
    """
    return Orchestrator(matcher=matcher, responder=responder, store=store)


def handle_request(orch: Orchestrator, request: Request) -> Response:
    """Main request processing flow.
    
    Flow:
    1. Match the request against registered rules
    2. If no match: return 404 Not Found response
    3. If match found:
       - Check for X-Session-ID header
       - Get or create session
       - Build response using template
       - Add X-Session-ID header to response
    
    Args:
        orch: The Orchestrator containing all components
        request: The incoming HTTP request
        
    Returns:
        HTTP Response with appropriate status, headers, and body
    """
    # Step 1: Match the request
    match_rule = orch.matcher.match_request(request)
    
    # Step 2: If no match, return 404
    if match_rule is None:
        return Response(
            status=404,
            headers={},
            body=b"Not Found"
        )
    
    # Step 3: Handle session
    session: Session
    session_id_header = request.headers.get("X-Session-ID")
    
    if session_id_header:
        # Try to get existing session
        existing_session = get_session(orch.store, session_id_header)
        if existing_session:
            session = existing_session
        else:
            # Session ID provided but not found - create new session
            session = create_session(orch.store, ttl_seconds=3600)
    else:
        # No session ID - create new session
        session = create_session(orch.store, ttl_seconds=3600)
    
    # Step 4: Build response
    response = build_response(orch.responder, match_rule, request, session)
    
    # Step 5: Add session ID header to response
    response_headers = dict(response.headers)
    response_headers["X-Session-ID"] = session.id
    
    return Response(
        status=response.status,
        headers=response_headers,
        body=response.body
    )
