"""
Tests for Analyst Role Implementation

Validates three-scope analyst routing, findings persistence, and escalation.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from a7_rt_core.context.core import analyst_view
from a7_rt_core.core.models import (
    DispatchAction,
    Node,
    NodeStatus,
    Project,
    Stage,
    SubagentReturn,
    SuspensionReason,
    SuspensionType,
)
from a7_rt_core.llm.parser import build_action, parse_manager_action
from a7_rt_core.llm.subagent import Subagent


class TestAnalystDispatchAction:
    """Test DispatchAction supports analyst scope and query parameters."""

    def test_dispatch_action_analyst_defaults(self):
        """Analyst dispatch without scope defaults to node scope."""
        action = DispatchAction(
            node_id="auth.jwt",
            role="analyst",
        )
        assert action.node_id == "auth.jwt"
        assert action.role == "analyst"
        assert action.scope is None
        assert action.query is None
        assert action.target_nodes == []

    def test_dispatch_action_analyst_with_scope(self):
        """Analyst dispatch with full parameters."""
        action = DispatchAction(
            node_id="auth.jwt",
            role="analyst",
            scope="node",
            query="Is verify() safe?",
            target_nodes=["auth.jwt"],
        )
        assert action.scope == "node"
        assert action.query == "Is verify() safe?"
        assert action.target_nodes == ["auth.jwt"]

    def test_dispatch_action_analyst_project_scope(self):
        """Analyst project scope with multiple targets."""
        action = DispatchAction(
            node_id="auth.check",
            role="analyst",
            scope="project",
            query="Do JWT implementations diverge?",
            target_nodes=["auth.jwt", "auth.handler", "auth.middleware"],
        )
        assert action.scope == "project"
        assert action.target_nodes == ["auth.jwt", "auth.handler", "auth.middleware"]

    def test_dispatch_action_analyst_external_scope(self):
        """Analyst external scope may have empty target_nodes."""
        action = DispatchAction(
            node_id="openapi.check",
            role="analyst",
            scope="external",
            query="What breaks in OpenAPI 4.0?",
            target_nodes=[],
        )
        assert action.scope == "external"
        assert action.target_nodes == []


class TestAnalystView:
    """Test analyst_view context assembly for three scopes."""

    def test_analyst_view_node_scope(self):
        """Node scope returns single node with dependencies."""
        doc = {
            "nodes": {
                "auth.jwt": {
                    "node_id": "auth.jwt",
                    "status": "grounded",
                    "interface": {"exports": ["verify(token)"]},
                },
                "auth.handler": {
                    "node_id": "auth.handler",
                    "status": "near",
                    "interface": {"exports": ["handle(req)"]},
                },
            },
            "dependencies": [
                {
                    "from_node": "auth.handler",
                    "to_node": "auth.jwt",
                    "type": "structural",
                },
            ],
            "graveyard": [],
        }

        view = analyst_view(doc, "Is verify() safe?", ["auth.jwt"])

        assert view["role"] == "analyst"
        assert view["query"] == "Is verify() safe?"
        assert "auth.jwt" in view["nodes"]
        assert view["nodes"]["auth.jwt"]["status"] == "grounded"
        # Should include dependency subgraph
        assert len(view["dependencies"]) == 1
        assert view["dependencies"][0]["from_node"] == "auth.handler"

    def test_analyst_view_project_scope(self):
        """Project scope returns multiple nodes."""
        doc = {
            "nodes": {
                "auth.jwt": {"node_id": "auth.jwt", "status": "grounded"},
                "auth.handler": {"node_id": "auth.handler", "status": "grounded"},
                "auth.middleware": {"node_id": "auth.middleware", "status": "near"},
            },
            "dependencies": [],
            "graveyard": [],
        }

        view = analyst_view(
            doc,
            "Do JWT implementations diverge?",
            ["auth.jwt", "auth.handler", "auth.middleware"],
        )

        assert len(view["nodes"]) == 3
        assert "auth.jwt" in view["nodes"]
        assert "auth.handler" in view["nodes"]
        assert "auth.middleware" in view["nodes"]

    def test_analyst_view_includes_tombstones(self):
        """Analyst view includes graveyard for pattern analysis."""
        doc = {
            "nodes": {
                "auth.jwt": {"node_id": "auth.jwt", "status": "grounded"},
            },
            "dependencies": [],
            "graveyard": [
                {
                    "node_id": "auth.old",
                    "reason": "replaced",
                    "timestamp": "2024-01-01",
                },
            ],
        }

        view = analyst_view(doc, "Check patterns", ["auth.jwt"])

        assert "tombstones" in view
        assert len(view["tombstones"]) == 1


class TestAnalystParseReturn:
    """Test SubagentReturn parsing for analyst role."""

    def test_parse_analyst_new_schema_node_scope(self):
        """Parse analyst return with new three-scope schema."""
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "node",
                "target": "auth.jwt",
                "target_nodes": ["auth.jwt"],
                "findings": {
                    "summary": "JWT verify() deviates from contract",
                    "details": {
                        "contract_fidelity": "deviates",
                        "contradictions": ["raises AuthError not ValueError"],
                        "patterns": [],
                        "risks": ["Callers miss exceptions"],
                    },
                },
                "confidence": "examined",
                "escalate": False,
                "sources": [{"type": "file", "ref": "auth/jwt.py", "credibility": "high"}],
                "checklist_suggestions": [],
            },
            "pr_note": "auth.jwt.verify() raises AuthError instead of ValueError",
        }

        # Call _parse_analyst directly - it's a pure function for this parsing
        from a7_rt_core.llm.subagent import _wild_suspension

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.PROVISIONAL
        assert result.role == "analyst"
        assert result.analysis_result is not None
        assert result.analysis_result["scope"] == "node"
        assert result.analysis_result["confidence"] == "examined"
        assert result.escalate == False
        assert result.pr_note == "auth.jwt.verify() raises AuthError instead of ValueError"

    def test_parse_analyst_project_scope_with_suggestions(self):
        """Parse project scope with checklist suggestions."""
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "project",
                "target": None,
                "target_nodes": ["auth.jwt", "auth.handler"],
                "findings": {
                    "summary": "Error handling diverges across auth layer",
                    "details": {
                        "contract_fidelity": "divergent",
                        "contradictions": ["Different exception types"],
                        "patterns": ["No consistent taxonomy"],
                        "risks": ["Silent failures possible"],
                    },
                },
                "confidence": "inferred",
                "escalate": False,
                "sources": [
                    {"type": "file", "ref": "auth/jwt.py", "credibility": "high"},
                    {"type": "file", "ref": "auth/handler.py", "credibility": "high"},
                ],
                "checklist_suggestions": [
                    {
                        "text": "Standardize auth error taxonomy",
                        "rationale": "Divergence creates silent failure risk",
                    }
                ],
            },
            "pr_note": "Cross-node analysis shows error handling divergence",
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.check")

        assert result.analysis_result["scope"] == "project"
        assert len(result.analysis_result["checklist_suggestions"]) == 1
        assert (
            result.analysis_result["checklist_suggestions"][0]["text"]
            == "Standardize auth error taxonomy"
        )

    def test_parse_analyst_escalation(self):
        """Parse analyst return with escalation flag."""
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "node",
                "target": "auth.jwt",
                "target_nodes": ["auth.jwt"],
                "findings": {
                    "summary": "Security vulnerability detected",
                    "details": {
                        "contract_fidelity": "n/a",
                        "contradictions": [],
                        "patterns": ["CVE-2022-23529 pattern"],
                        "risks": ["Algorithm confusion attack"],
                    },
                },
                "confidence": "examined",
                "escalate": True,
                "sources": [{"type": "file", "ref": "auth/jwt.py:42", "credibility": "high"}],
                "checklist_suggestions": [],
            },
            "pr_note": "SECURITY RISK: Algorithm confusion vulnerability",
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.escalate == True
        assert result.analysis_result["escalate"] == True

    def test_parse_analyst_suspension(self):
        """Parse analyst return with suspension."""
        data = {
            "status": "suspended",
            "suspension_reason": {"type": "near", "detail": "Need auth.jwt source"},
            "analysis_result": {
                "scope": "node",
                "target": "auth.handler",
                "target_nodes": ["auth.handler"],
                "findings": {"summary": "Cannot verify without auth.jwt source"},
                "confidence": "examined",
                "escalate": False,
                "sources": [],
                "checklist_suggestions": [],
            },
            "pr_note": "Cannot verify handler without jwt source",
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.handler")

        assert result.status == NodeStatus.SUSPENDED
        assert result.suspension_reason is not None
        assert result.suspension_reason.type == SuspensionType.NEAR

    def test_parse_analyst_missing_scope_fails(self):
        """Analyst return without scope is wild suspension."""
        data = {
            "status": "provisional",
            "analysis_result": {
                # Missing scope
                "target_nodes": ["auth.jwt"],
                "findings": {},
                "confidence": "examined",
            },
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.SUSPENDED
        assert result.suspension_reason.type == SuspensionType.WILD

    def test_parse_analyst_invalid_confidence_fails(self):
        """Analyst return with invalid confidence is wild suspension."""
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "node",
                "target_nodes": ["auth.jwt"],
                "findings": {},
                "confidence": "invalid_confidence",  # Invalid
            },
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.SUSPENDED
        assert result.suspension_reason.type == SuspensionType.WILD


class TestAnalystManagerParser:
    """Test manager parser handles analyst dispatch with scope."""

    def test_parse_dispatch_analyst_node_scope(self):
        """Parse DISPATCH analyst with node scope."""
        raw = """
        ACT: {
            "action": "DISPATCH",
            "node_id": "auth.jwt",
            "role": "analyst",
            "scope": "node",
            "query": "Is verify() safe to depend on?"
        }
        """

        action = parse_manager_action(raw)

        assert isinstance(action, DispatchAction)
        assert action.node_id == "auth.jwt"
        assert action.role == "analyst"
        assert action.scope == "node"
        assert action.query == "Is verify() safe to depend on?"

    def test_parse_dispatch_analyst_project_scope(self):
        """Parse DISPATCH analyst with project scope."""
        raw = """
        ACT: {
            "action": "DISPATCH",
            "node_id": "auth.check",
            "role": "analyst",
            "scope": "project",
            "query": "Do JWT implementations diverge?",
            "target_nodes": ["auth.jwt", "auth.handler", "auth.middleware"]
        }
        """

        action = parse_manager_action(raw)

        assert isinstance(action, DispatchAction)
        assert action.scope == "project"
        assert action.target_nodes == ["auth.jwt", "auth.handler", "auth.middleware"]

    def test_build_action_analyst_params(self):
        """build_action passes analyst params correctly."""
        data = {
            "action": "DISPATCH",
            "node_id": "auth.jwt",
            "role": "analyst",
            "scope": "external",
            "query": "OpenAPI 4.0 breaking changes",
            "target_nodes": [],
        }

        action = build_action(data)

        assert action.scope == "external"
        assert action.query == "OpenAPI 4.0 breaking changes"
        assert action.target_nodes == []


class TestSubagentReturnAnalysisResult:
    """Test SubagentReturn model with analysis_result field."""

    def test_subagent_return_has_analysis_result(self):
        """SubagentReturn accepts analysis_result parameter."""
        analysis = {
            "scope": "node",
            "target": "auth.jwt",
            "target_nodes": ["auth.jwt"],
            "findings": {"summary": "Test findings"},
            "confidence": "examined",
        }

        result = SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            role="analyst",
            analysis_result=analysis,
        )

        assert result.analysis_result == analysis

    def test_subagent_return_defaults_empty_analysis_result(self):
        """SubagentReturn defaults analysis_result to empty dict."""
        result = SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            role="builder",
        )

        assert result.analysis_result == {}


class TestAnalystNodeMetadata:
    """Test analyst_findings persistence in node metadata."""

    def test_node_metadata_has_analyst_findings_field(self):
        """NodeMetadata model includes analyst_findings field (list[dict] schema)."""
        from a7_rt_core.core.models import NodeMetadata

        # Default is empty list
        meta = NodeMetadata(
            tokens=100,
            lines=10,
            content_hash="abc123",
            first_export_preview="def test()",
            plumbing_summary="Test function",
        )

        assert meta.analyst_findings == []

        # Can be set as list
        findings = {
            "scope": "node",
            "confidence": "examined",
            "findings": {"summary": "Test"},
        }
        meta_with_findings = NodeMetadata(
            tokens=100,
            lines=10,
            content_hash="abc123",
            first_export_preview="def test()",
            plumbing_summary="Test function",
            analyst_findings=[findings],
        )

        assert meta_with_findings.analyst_findings == [findings]

        # Legacy dict is coerced to list
        meta_with_dict = NodeMetadata(
            tokens=100,
            lines=10,
            content_hash="abc123",
            first_export_preview="def test()",
            plumbing_summary="Test function",
            analyst_findings=findings,  # Single dict (legacy)
        )

        assert meta_with_dict.analyst_findings == [findings]


# ---------------------------------------------------------------------------
# Fan-in Detection Tests
# ---------------------------------------------------------------------------


class TestFanInDetection:
    """Test fan-in node detection for auto-triggered analyst audits."""

    def test_fan_in_nodes_basic(self):
        """Detect nodes with multiple incoming dependencies."""
        from a7_rt_core.core.graph import fan_in_nodes

        nodes = {"A": {}, "B": {}, "C": {}, "D": {}}
        deps = [
            {"from_node": "C", "to_node": "A", "type": "structural"},
            {"from_node": "D", "to_node": "A", "type": "structural"},
            {"from_node": "B", "to_node": "C", "type": "structural"},
        ]

        result = fan_in_nodes(nodes, deps, threshold=2)

        assert "A" in result
        assert "B" not in result
        assert "C" not in result
        assert "D" not in result

    def test_fan_in_nodes_threshold(self):
        """Respect threshold parameter."""
        from a7_rt_core.core.graph import fan_in_nodes

        nodes = {"A": {}, "B": {}, "C": {}}
        deps = [
            {"from_node": "B", "to_node": "A", "type": "structural"},
            {"from_node": "C", "to_node": "A", "type": "structural"},
        ]

        # threshold=2 should find A
        result_2 = fan_in_nodes(nodes, deps, threshold=2)
        assert "A" in result_2

        # threshold=3 should not find A
        result_3 = fan_in_nodes(nodes, deps, threshold=3)
        assert "A" not in result_3

    def test_fan_in_candidates_for_audit(self):
        """Generate audit candidates with context."""
        from a7_rt_core.core.graph import fan_in_candidates_for_audit

        nodes = {
            "A": {"status": "near"},
            "B": {"status": "grounded"},
            "C": {"status": "near"},
            "D": {"status": "near"},
        }
        deps = [
            {"from_node": "C", "to_node": "A", "type": "structural"},
            {"from_node": "D", "to_node": "A", "type": "structural"},
        ]

        candidates = fan_in_candidates_for_audit(nodes, deps, threshold=2)

        assert len(candidates) == 1
        assert candidates[0]["node_id"] == "A"
        assert candidates[0]["prereq_count"] == 2
        assert set(candidates[0]["prereq_nodes"]) == {"C", "D"}
        assert "suggested_query" in candidates[0]
        assert "target_nodes" in candidates[0]

    def test_fan_in_candidates_excludes_grounded(self):
        """Filter out grounded nodes."""
        from a7_rt_core.core.graph import fan_in_candidates_for_audit

        nodes = {
            "A": {"status": "grounded"},  # Should be excluded
            "B": {"status": "near"},
            "C": {"status": "near"},
        }
        deps = [
            {"from_node": "B", "to_node": "A", "type": "structural"},
            {"from_node": "C", "to_node": "A", "type": "structural"},
        ]

        candidates = fan_in_candidates_for_audit(nodes, deps, threshold=2)

        assert len(candidates) == 0

    def test_fan_in_candidates_custom_exclude(self):
        """Respect custom exclude_statuses."""
        from a7_rt_core.core.graph import fan_in_candidates_for_audit

        nodes = {
            "A": {"status": "provisional"},
            "B": {"status": "near"},
            "C": {"status": "near"},
        }
        deps = [
            {"from_node": "B", "to_node": "A", "type": "structural"},
            {"from_node": "C", "to_node": "A", "type": "structural"},
        ]

        # Exclude provisional
        candidates = fan_in_candidates_for_audit(
            nodes, deps, threshold=2, exclude_statuses={"grounded", "provisional"}
        )

        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# MCP Tools Tests
# ---------------------------------------------------------------------------


class TestMCPTools:
    """Test MCP tool stub module."""

    def test_mcp_tool_set_creation(self):
        """Create MCPToolSet without client."""
        from a7_rt_core.tools.mcp import MCPToolSet

        tools = MCPToolSet()
        assert tools.is_implemented() is False

    def test_mcp_list_tools(self):
        """List available tools."""
        from a7_rt_core.tools.mcp import MCPToolSet

        tools = MCPToolSet()
        tool_list = tools.list_tools()

        assert len(tool_list) == 4
        names = {t["name"] for t in tool_list}
        assert "web_search" in names
        assert "fetch_docs" in names
        assert "parse_changelog" in names
        assert "security_advisory" in names

    def test_mcp_web_search_stub(self):
        """Web search returns void result when not implemented."""
        import asyncio

        from a7_rt_core.tools.mcp import MCPToolSet

        async def test():
            tools = MCPToolSet()
            result = await tools.execute("web_search", "OpenAPI 4.0")

            assert result.source_type == "none"
            assert "[≋ VOID]" in result.content
            assert result.credibility == "low"

        asyncio.run(test())

    def test_mcp_research_external_stub(self):
        """research_external returns void findings when not implemented."""
        import asyncio

        from a7_rt_core.tools.mcp import research_external

        async def test():
            result = await research_external("OpenAPI 4.0 breaking changes")

            assert result["scope"] == "external"
            assert result["confidence"] == "sourced"
            assert "[≋ VOID]" in result["findings"]["summary"]
            assert len(result["sources"]) == 0

        asyncio.run(test())

    def test_mcp_research_result_to_analyst_source(self):
        """Convert ResearchResult to analyst source format."""
        from a7_rt_core.tools.mcp import ResearchResult

        result = ResearchResult(
            query="test",
            source_type="web",
            content="content",
            url="https://example.com",
            credibility="high",
        )

        source = result.to_analyst_source()
        assert source["type"] == "web"
        assert source["ref"] == "https://example.com"
        assert source["credibility"] == "high"

    def test_mcp_unknown_tool(self):
        """Unknown tool returns error result."""
        import asyncio

        from a7_rt_core.tools.mcp import MCPToolSet

        async def test():
            tools = MCPToolSet()
            result = await tools.execute("unknown_tool", "query")

            assert result.source_type == "none"
            assert "Unknown tool" in result.content

        asyncio.run(test())
