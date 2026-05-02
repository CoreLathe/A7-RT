"""
Phase 9.4 Test: Subagent Hook

Simulates agent behavior for testing builder retry context:
- Builder first attempt: Creates auth_handler with intentional contract deviation
- Analyst: Reports the jwt.verify() vs verify_token() mismatch
- Builder retry: Fixes to use correct method name

Returns SubagentReturn with appropriate status and content.
"""

import json
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import NodeStatus, SubagentReturn


def phase94_subagent_hook(
    node_id: str,
    role: str,
    context: dict,
    session_id: str = None,
    on_tool_call=None,
    shadow=None,
) -> SubagentReturn:
    """
    Simulate subagent responses for phase 9.4 test.

    Context inspection: Logs what the builder sees on retry
    to verify analyst_findings and exploration_hints are present.
    """

    # Log context inspection for telemetry
    _log_context_inspection(node_id, role, context)

    if role == "builder" and node_id == "auth_handler":
        return _handle_auth_handler_builder(context, shadow)

    elif role == "analyst" and node_id == "auth_handler":
        return _handle_auth_handler_analyst(context)

    else:
        # Default fallback
        return SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            files={f"{node_id}.py": f"# Default implementation for {node_id}"},
            role=role,
            pr_note="Default fallback response",
        )


def _log_context_inspection(node_id: str, role: str, context: dict):
    """Log what context is visible to the agent for verification."""

    inspection = {
        "node_id": node_id,
        "role": role,
        "has_analyst_findings": False,
        "has_exploration_hints": False,
        "exploration_hints": None,
        "analyst_findings": None,
    }

    # Check for analyst_findings in target metadata
    target = context.get("target", {})
    metadata = target.get("metadata", {})

    if metadata and "analyst_findings" in metadata:
        inspection["has_analyst_findings"] = True
        findings = metadata["analyst_findings"]
        # Truncate for logging
        if isinstance(findings, list) and findings:
            inspection["analyst_findings"] = {
                "scope": findings[-1].get("scope"),
                "has_findings": bool(findings[-1].get("findings")),
            }
        elif isinstance(findings, dict):
            inspection["analyst_findings"] = {
                "scope": findings.get("scope"),
                "has_findings": bool(findings.get("findings")),
            }

    # Also check top-level analyst_findings in view
    if "analyst_findings" in context:
        inspection["has_analyst_findings"] = True
        af = context["analyst_findings"]
        inspection["analyst_findings_summary"] = str(af)[:200] if af else None

    # Check for exploration_hints
    if "exploration_hints" in context:
        inspection["has_exploration_hints"] = True
        hints = context["exploration_hints"]
        inspection["exploration_hints"] = {
            "previous_attempts": hints.get("previous_attempts", 0),
            "files_written_count": len(hints.get("files_written", [])),
        }

    # Log to stderr for telemetry capture
    print(
        f"[SUBAGENT_CONTEXT] {json.dumps(inspection)}",
        file=sys.stderr,
    )


def _handle_auth_handler_builder(context: dict, shadow) -> SubagentReturn:
    """
    Simulate builder for auth_handler.

    First attempt: Uses jwt.verify() (wrong - should be verify_token())
    Retry: Uses jwt.verify_token() (correct)
    """

    # Detect if this is a retry via exploration_hints
    exploration_hints = context.get("exploration_hints", {})
    previous_attempts = exploration_hints.get("previous_attempts", 0)

    # Check for analyst findings
    analyst_findings = context.get("analyst_findings")
    findings_available = bool(analyst_findings)

    # Get manager guidance
    manager_guidance = context.get("manager_guidance", {})
    manager_note = manager_guidance.get("manager_note", "")

    # Get structural deps info
    structural_deps = context.get("structural_deps", {})
    jwt_util = structural_deps.get("jwt_util", {})
    jwt_exports = []
    if jwt_util and "interface" in jwt_util:
        jwt_exports = jwt_util["interface"].get("exports", [])

    print(
        f"[BUILDER] auth_handler - previous_attempts={previous_attempts}, "
        f"findings_available={findings_available}, exports={jwt_exports}",
        file=sys.stderr,
    )

    # Detect retry by checking if analyst findings are available
    # Note: exploration_hints.previous_attempts may be 0 on redispatch due to harness
    # inconsistency, so we use findings_available as the primary retry signal
    if previous_attempts == 0 and not findings_available:
        # === FIRST ATTEMPT: Intentional bug ===
        # Create auth_handler that calls wrong method name

        code = '''"""
Auth Handler Module
Handles authentication using JWT tokens.
"""

import jwt_util


def authenticate_token(token: str) -> dict:
    """
    Authenticate a JWT token.

    Args:
        token: JWT token string

    Returns:
        dict with user info if valid

    Raises:
        ValueError: If token is invalid
    """
    # BUG: Using wrong method name - jwt_util has verify_token not verify
    try:
        payload = jwt_util.verify(token)  # WRONG: should be verify_token()
        return {"valid": True, "payload": payload}
    except Exception as e:
        raise ValueError(f"Invalid token: {e}")


def revoke_token(token_id: str) -> bool:
    """Revoke a token by ID."""
    return True
'''

        # Also create a test that will fail
        test_code = '''"""
Tests for auth_handler
"""

import auth_handler


def test_authenticate_token():
    """Test token authentication."""
    # This will fail because verify() doesn't exist
    result = auth_handler.authenticate_token("valid.token.here")
    assert result["valid"] == True
'''

        return SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            files={
                "auth/handler.py": code,
                "auth/handler_test.py": test_code,
            },
            role="builder",
            pr_note="First attempt: Implement auth handler with JWT integration. Uses jwt.verify() method.",
            iterations_used=5,
        )

    else:
        # === RETRY ATTEMPT: Fix the bug ===
        # Should use correct method name based on analyst findings

        code = '''"""
Auth Handler Module
Handles authentication using JWT tokens.
"""

import jwt_util


def authenticate_token(token: str) -> dict:
    """
    Authenticate a JWT token.

    Args:
        token: JWT token string

    Returns:
        dict with user info if valid

    Raises:
        ValueError: If token is invalid
    """
    # FIXED: Using correct method name verify_token()
    try:
        payload = jwt_util.verify_token(token)  # CORRECT
        return {"valid": True, "payload": payload}
    except Exception as e:
        raise ValueError(f"Invalid token: {e}")


def revoke_token(token_id: str) -> bool:
    """Revoke a token by ID."""
    return True
'''

        test_code = '''"""
Tests for auth_handler
"""

from unittest.mock import patch, MagicMock
import auth_handler


def test_authenticate_token():
    """Test token authentication."""
    # Mock the jwt_util.verify_token call
    mock_payload = {"user_id": "123", "exp": 1234567890}

    with patch.object(auth_handler.jwt_util, 'verify_token', return_value=mock_payload):
        result = auth_handler.authenticate_token("valid.token.here")
        assert result["valid"] == True
        assert result["payload"] == mock_payload
'''

        # Determine status based on whether we had findings
        # If findings were available, we can succeed
        if findings_available:
            status = NodeStatus.GROUNDED
            pr_note = "Retry: Fixed contract deviation. Now using jwt_util.verify_token() as per analyst findings."
        else:
            # Without findings, we might still fail
            status = NodeStatus.PROVISIONAL
            pr_note = "Retry attempt made, but analyst findings not visible in context."

        # Include interface_update for GROUNDED status
        interface_update = (
            {
                "exports": ["authenticate_token", "revoke_token"],
                "raises": ["ValueError"],
            }
            if status == NodeStatus.GROUNDED
            else {}
        )

        return SubagentReturn(
            status=status,
            files={
                "auth/handler.py": code,
                "auth/handler_test.py": test_code,
            },
            interface_update=interface_update,
            role="builder",
            pr_note=pr_note,
            iterations_used=8,
        )


def _handle_auth_handler_analyst(context: dict) -> SubagentReturn:
    """
    Simulate analyst examining auth_handler.

    Identifies contract deviation: auth_handler calls verify()
    but jwt_util exports verify_token().
    """

    # Get target node info
    target = context.get("target", {})
    target_id = target.get("node_id", "unknown")

    # Get structural deps to check exports
    query_nodes = context.get("query_nodes", [])

    # Find jwt_util in query nodes
    jwt_util_node = None
    auth_handler_node = None

    for node in query_nodes:
        node_id = node.get("node_id", "")
        if "jwt" in node_id.lower():
            jwt_util_node = node
        if "handler" in node_id.lower() or "auth_handler" in node_id:
            auth_handler_node = node

    # Check what jwt_util exports
    jwt_interface = jwt_util_node.get("interface", {}) if jwt_util_node else {}
    jwt_exports = jwt_interface.get("exports", []) if jwt_interface else []

    # Simulate finding the contract deviation
    findings = {
        "summary": f"Contract deviation detected in {target_id}",
        "issues": [
            {
                "type": "contract_deviation",
                "severity": "high",
                "description": "auth_handler calls jwt_util.verify() but jwt_util exports verify_token()",
                "expected": "jwt_util.verify_token(token)",
                "actual": "jwt_util.verify(token)",
                "fix": "Change verify() to verify_token() in auth_handler.authenticate_token()",
            }
        ],
        "recommendations": [
            "Update auth_handler to use jwt_util.verify_token() method",
            "Ensure test mocks verify_token not verify",
        ],
    }

    print(
        f"[ANALYST] Examined {target_id} - found {len(findings['issues'])} issues",
        file=sys.stderr,
    )

    return SubagentReturn(
        status=NodeStatus.NEAR,  # Analyst findings don't change node status
        role="analyst",
        analysis_result={
            "scope": "node",
            "target": target_id,
            "target_nodes": [target_id],
            "findings": findings,
            "confidence": "high",
            "sources": ["auth/handler.py", "jwt_util interface"],
            "escalate": False,
        },
        pr_note="Analysis complete: Found contract deviation between jwt_util exports and auth_handler usage.",
        iterations_used=3,
    )


if __name__ == "__main__":
    print("Phase 9.4 subagent hook - import and use with harness")
