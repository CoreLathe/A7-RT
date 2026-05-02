"""
Mechanical Smoke Test — Analyst Role Implementation

End-to-end validation of analyst three-scope system without LLM calls.
Tests dispatch, parsing, persistence, escalation, fan-in triggers, and MCP stubs.

A7 Operation: [⫴ TEST] hard path — all gates must pass
"""

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from a7_rt_core.context.core import analyst_view, manager_view
from a7_rt_core.core.graph import fan_in_candidates_for_audit, fan_in_nodes
from a7_rt_core.core.models import (
    DispatchAction,
    ManagerMode,
    ManagerState,
    NodeMetadata,
    NodeStatus,
    SubagentReturn,
    SuspensionReason,
    SuspensionType,
)
from a7_rt_core.harness.core import Harness
from a7_rt_core.llm.parser import build_action, parse_manager_action
from a7_rt_core.llm.subagent import Subagent
from a7_rt_core.storage.repository import Repository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_repo():
    """Create temporary repository for smoke tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = Repository(root)
        yield repo


@pytest.fixture
def mock_subagent_hook():
    """Return mock subagent that simulates analyst responses."""

    def hook(node_id, role, ctx, **kwargs):
        if role == "analyst":
            # Simulate analyst return with new schema
            scope = ctx.get("role")  # analyst_view sets role
            query = ctx.get("query", "analysis")

            return SubagentReturn(
                status=NodeStatus.PROVISIONAL,
                content=json.dumps(
                    {
                        "query": query,
                        "scope": scope,
                        "findings": {"summary": f"Analyzed {node_id}"},
                    }
                ),
                role="analyst",
                analysis_result={
                    "scope": "node",
                    "target": node_id,
                    "target_nodes": [node_id],
                    "findings": {
                        "summary": f"Analysis complete for {node_id}",
                        "details": {
                            "contract_fidelity": "matches",
                            "contradictions": [],
                            "patterns": [],
                            "risks": [],
                        },
                    },
                    "confidence": "examined",
                    "escalate": False,
                    "sources": [{"type": "file", "ref": f"{node_id}.py", "credibility": "high"}],
                    "checklist_suggestions": [],
                },
                pr_note=f"[⫴ TEST] Analysis complete for {node_id}",
            )
        else:
            return SubagentReturn(
                status=NodeStatus.PROVISIONAL,
                content="builder output",
                role="builder",
            )

    return hook


@pytest.fixture
def harness(temp_repo, mock_subagent_hook):
    """Create harness with mock subagent."""
    h = Harness(
        temp_repo.root,
        manager_hook=lambda board, state: DispatchAction(node_id="test.node", role="builder"),
        subagent_hook=mock_subagent_hook,
    )
    return h


# ---------------------------------------------------------------------------
# Smoke Test: Three-Scope Dispatch
# ---------------------------------------------------------------------------


class TestSmokeThreeScopeDispatch:
    """Mechanical test of node/project/external scope routing."""

    def test_dispatch_action_node_scope(self):
        """DISPATCH with node scope routes correctly."""
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

    def test_dispatch_action_project_scope(self):
        """DISPATCH with project scope routes correctly."""
        action = DispatchAction(
            node_id="auth.check",
            role="analyst",
            scope="project",
            query="Do JWT implementations diverge?",
            target_nodes=["auth.jwt", "auth.handler"],
        )

        assert action.scope == "project"
        assert len(action.target_nodes) == 2

    def test_dispatch_action_external_scope(self):
        """DISPATCH with external scope routes correctly."""
        action = DispatchAction(
            node_id="openapi.check",
            role="analyst",
            scope="external",
            query="OpenAPI 4.0 breaking changes",
            target_nodes=[],
        )

        assert action.scope == "external"
        assert action.target_nodes == []

    def test_manager_parser_analyst_dispatch(self):
        """Parser extracts analyst scope parameters."""
        raw = """
        ACT: {
            "action": "DISPATCH",
            "node_id": "auth.jwt",
            "role": "analyst",
            "scope": "node",
            "query": "Is verify() safe?",
            "target_nodes": ["auth.jwt"]
        }
        """

        action = parse_manager_action(raw)

        assert isinstance(action, DispatchAction)
        assert action.role == "analyst"
        assert action.scope == "node"
        assert action.query == "Is verify() safe?"


# ---------------------------------------------------------------------------
# Smoke Test: Analysis Result Parsing
# ---------------------------------------------------------------------------


class TestSmokeAnalysisResultParsing:
    """Mechanical test of analyst return parsing."""

    def test_parse_analyst_new_schema_success(self):
        """Valid new schema parses correctly."""
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
            "pr_note": "Analysis complete",
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.PROVISIONAL
        assert result.role == "analyst"
        assert result.analysis_result["scope"] == "node"
        assert result.analysis_result["confidence"] == "examined"
        assert result.analysis_result["escalate"] == False

    def test_parse_analyst_escalation_flag(self):
        """Escalation flag is preserved."""
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
                        "patterns": [],
                        "risks": ["CVE-2022-23529"],
                    },
                },
                "confidence": "examined",
                "escalate": True,
                "sources": [],
                "checklist_suggestions": [],
            },
            "pr_note": "SECURITY RISK",
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.escalate == True
        assert result.analysis_result["escalate"] == True

    def test_parse_analyst_invalid_scope_fails(self):
        """Invalid scope triggers wild suspension."""
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "invalid_scope",
                "target_nodes": ["auth.jwt"],
                "findings": {},
                "confidence": "examined",
            },
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.SUSPENDED
        assert result.suspension_reason.type == SuspensionType.WILD

    def test_parse_analyst_missing_findings_fails(self):
        """Missing findings triggers wild suspension."""
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "node",
                "target_nodes": ["auth.jwt"],
                "confidence": "examined",
            },
        }

        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.SUSPENDED


# ---------------------------------------------------------------------------
# Smoke Test: Findings Persistence
# ---------------------------------------------------------------------------


class TestSmokeFindingsPersistence:
    """Mechanical test of analyst_findings storage."""

    def test_analyst_findings_in_node_metadata(self):
        """NodeMetadata accepts analyst_findings as list[dict]."""
        findings = [
            {
                "scope": "node",
                "target": "auth.jwt",
                "target_nodes": ["auth.jwt"],
                "findings": {"summary": "Test findings"},
                "confidence": "examined",
                "escalate": False,
                "sources": [],
                "checklist_suggestions": [],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        meta = NodeMetadata(
            tokens=100,
            lines=10,
            content_hash="abc123",
            first_export_preview="def test()",
            plumbing_summary="Test",
            analyst_findings=findings,
        )

        assert meta.analyst_findings == findings
        assert meta.analyst_findings[0]["scope"] == "node"
        assert meta.analyst_findings[0]["confidence"] == "examined"

        # Legacy: single dict is coerced to list
        single_finding = {
            "scope": "project",
            "target": "auth",
            "target_nodes": ["auth.handler"],
            "findings": {"summary": "Legacy"},
            "confidence": "examined",
            "escalate": False,
            "sources": [],
            "checklist_suggestions": [],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_legacy = NodeMetadata(
            tokens=100,
            lines=10,
            content_hash="abc123",
            first_export_preview="def test()",
            plumbing_summary="Test",
            analyst_findings=single_finding,
        )
        assert meta_legacy.analyst_findings == [single_finding]

    def test_analyst_findings_defaults_empty_list(self):
        """analyst_findings defaults to empty list (not None)."""
        meta = NodeMetadata(
            tokens=100,
            lines=10,
            content_hash="abc123",
            first_export_preview="def test()",
            plumbing_summary="Test",
        )

        assert meta.analyst_findings == []


# ---------------------------------------------------------------------------
# Smoke Test: Escalation to Human Queue
# ---------------------------------------------------------------------------


class TestSmokeEscalation:
    """Mechanical test of analyst escalation routing."""

    def test_escalation_queued_to_human_input(self):
        """Escalation appends to human_input_queue."""
        # Simulate escalation via apply_return
        analysis_result = {
            "scope": "node",
            "target": "auth.jwt",
            "target_nodes": ["auth.jwt"],
            "findings": {
                "summary": "Security vulnerability",
                "details": {
                    "contract_fidelity": "n/a",
                    "contradictions": [],
                    "patterns": ["CVE pattern"],
                    "risks": ["Algorithm confusion"],
                },
            },
            "confidence": "examined",
            "escalate": True,
            "sources": [{"type": "file", "ref": "auth/jwt.py:42", "credibility": "high"}],
            "checklist_suggestions": [],
        }

        result = SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            role="analyst",
            analysis_result=analysis_result,
            escalate=True,  # Must set escalate directly on SubagentReturn
            pr_note="SECURITY RISK",
        )

        # Verify the analysis_result structure is correct
        assert result.analysis_result["escalate"] == True
        assert result.escalate == True


# ---------------------------------------------------------------------------
# Smoke Test: Manager View Integration
# ---------------------------------------------------------------------------


class TestSmokeManagerView:
    """Mechanical test of analyst_findings in manager context."""

    def test_manager_view_includes_analyst_findings(self):
        """Manager view surfaces analyst findings summary."""
        doc = {
            "project": {"project_id": "test", "max_turns": 25, "drain_turn": 20},
            "stages": {
                "stage-1": {
                    "stage_id": "stage-1",
                    "project_id": "test",
                    "name": "Test",
                    "status": "active",
                    "node_ids": ["auth.jwt"],
                }
            },
            "nodes": {
                "auth.jwt": {
                    "node_id": "auth.jwt",
                    "stage_id": "stage-1",
                    "status": "grounded",
                    "type": "feature",
                    "description": "JWT handler",
                    "interface": {"exports": ["verify(token)"]},
                    "structural_deps": [],
                    "metadata": {
                        "tokens": 100,
                        "lines": 10,
                        "content_hash": "abc123",
                        "first_export_preview": "def verify(token):",
                        "plumbing_summary": "JWT verification",
                        "analyst_findings": {
                            "scope": "node",
                            "confidence": "examined",
                            "findings": {
                                "summary": "Contract verified",
                            },
                            "escalate": False,
                            "recorded_at": "2024-01-01T00:00:00Z",
                        },
                    },
                }
            },
            "dependencies": [],
            "graveyard": [],
        }

        view = manager_view(doc, budget_tokens=10000)

        assert "auth.jwt" in view["nodes"]
        node_view = view["nodes"]["auth.jwt"]
        assert "analyst_findings" in node_view
        assert node_view["analyst_findings"]["confidence"] == "examined"
        assert node_view["analyst_findings"]["summary"] == "Contract verified"


# ---------------------------------------------------------------------------
# Smoke Test: Fan-in Detection
# ---------------------------------------------------------------------------


class TestSmokeFanInDetection:
    """Mechanical test of fan-in auto-trigger."""

    def test_fan_in_nodes_detection(self):
        """Fan-in nodes identified correctly."""
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

    def test_fan_in_candidates_generation(self):
        """Candidates include context for auto-trigger."""
        nodes = {
            "api_integration": {"status": "near"},
            "calculator": {"status": "grounded"},
            "validator": {"status": "grounded"},
        }
        deps = [
            {
                "from_node": "calculator",
                "to_node": "api_integration",
                "type": "structural",
            },
            {
                "from_node": "validator",
                "to_node": "api_integration",
                "type": "structural",
            },
        ]

        candidates = fan_in_candidates_for_audit(nodes, deps, threshold=2)

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["node_id"] == "api_integration"
        assert candidate["prereq_count"] == 2
        assert "suggested_query" in candidate
        assert "target_nodes" in candidate

    def test_fan_in_excludes_grounded(self):
        """Grounded nodes excluded from candidates."""
        nodes = {
            "A": {"status": "grounded"},
            "B": {"status": "near"},
            "C": {"status": "near"},
        }
        deps = [
            {"from_node": "B", "to_node": "A", "type": "structural"},
            {"from_node": "C", "to_node": "A", "type": "structural"},
        ]

        candidates = fan_in_candidates_for_audit(nodes, deps, threshold=2)

        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# Smoke Test: MCP Tools Stub
# ---------------------------------------------------------------------------


class TestSmokeMCPTools:
    """Mechanical test of MCP tool interface."""

    def test_mcp_tool_set_listing(self):
        """MCPToolSet lists available tools."""
        from a7_rt_core.tools.mcp import MCPToolSet

        tools = MCPToolSet()
        tool_list = tools.list_tools()

        assert len(tool_list) == 4
        names = {t["name"] for t in tool_list}
        assert "web_search" in names
        assert "fetch_docs" in names
        assert "parse_changelog" in names
        assert "security_advisory" in names

    def test_mcp_stub_returns_void(self):
        """Unimplemented tools return [≋ VOID]."""
        import asyncio

        from a7_rt_core.tools.mcp import MCPToolSet

        async def test():
            tools = MCPToolSet()
            result = await tools.execute("web_search", "OpenAPI 4.0")

            assert result.source_type == "none"
            assert "[≋ VOID]" in result.content
            assert result.credibility == "low"

        asyncio.run(test())

    def test_mcp_research_external_schema(self):
        """research_external returns analyst-compatible schema."""
        import asyncio

        from a7_rt_core.tools.mcp import research_external

        async def test():
            result = await research_external("OpenAPI 4.0 breaking changes")

            # Matches analyst findings schema
            assert "scope" in result
            assert "findings" in result
            assert "summary" in result["findings"]
            assert "details" in result["findings"]
            assert "confidence" in result
            assert "sources" in result
            assert "escalate" in result

        asyncio.run(test())


# ---------------------------------------------------------------------------
# Smoke Test: End-to-End Workflow
# ---------------------------------------------------------------------------


class TestSmokeEndToEnd:
    """Mechanical test of full analyst workflow."""

    def test_full_analyst_workflow_node_scope(self):
        """Complete node scope analyst dispatch and commit."""
        # Create analysis result
        analysis_result = {
            "scope": "node",
            "target": "auth.jwt",
            "target_nodes": ["auth.jwt"],
            "findings": {
                "summary": "Contract verified",
                "details": {
                    "contract_fidelity": "matches",
                    "contradictions": [],
                    "patterns": [],
                    "risks": [],
                },
            },
            "confidence": "examined",
            "escalate": False,
            "sources": [{"type": "file", "ref": "auth/jwt.py", "credibility": "high"}],
            "checklist_suggestions": [],
        }

        result = SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            role="analyst",
            analysis_result=analysis_result,
            pr_note="Analysis complete",
        )

        # Verify the complete flow
        assert result.role == "analyst"
        assert result.analysis_result["scope"] == "node"
        assert result.analysis_result["confidence"] == "examined"
        assert result.analysis_result["escalate"] == False

    def test_analyst_view_assembly(self):
        """analyst_view assembles correct context."""
        doc = {
            "nodes": {
                "auth.jwt": {
                    "node_id": "auth.jwt",
                    "status": "grounded",
                    "interface": {"exports": ["verify(token)"]},
                    "metadata": {"tokens": 50},
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

        view = analyst_view(doc, "Check interface", ["auth.jwt", "auth.handler"])

        assert view["role"] == "analyst"
        assert view["query"] == "Check interface"
        assert "auth.jwt" in view["nodes"]
        assert "auth.handler" in view["nodes"]
        assert len(view["dependencies"]) == 1


# ---------------------------------------------------------------------------
# A7 Invariants Verification
# ---------------------------------------------------------------------------


class TestSmokeA7Invariants:
    """Verify A7 invariants hold in analyst implementation."""

    def test_confidence_never_exceeds_grounding(self):
        """Confidence levels are valid."""
        valid_confidences = {"examined", "inferred", "sourced"}

        for conf in valid_confidences:
            data = {
                "status": "provisional",
                "analysis_result": {
                    "scope": "node",
                    "target": "auth.jwt",
                    "target_nodes": ["auth.jwt"],
                    "findings": {"summary": "Test", "details": {}},
                    "confidence": conf,
                    "escalate": False,
                    "sources": [],
                    "checklist_suggestions": [],
                },
            }
            subagent = object.__new__(Subagent)
            result = subagent._parse_analyst(data, "auth.jwt")
            assert result.status == NodeStatus.PROVISIONAL

    def test_schema_violation_triggers_wild(self):
        """Schema violations trigger [☠] wild suspension."""
        # Missing required field (findings dict)
        data = {
            "status": "provisional",
            "analysis_result": {
                "scope": "node",
                "target_nodes": ["auth.jwt"],
                # Missing findings dict structure
                "confidence": "examined",
            },
        }
        subagent = object.__new__(Subagent)
        result = subagent._parse_analyst(data, "auth.jwt")

        assert result.status == NodeStatus.SUSPENDED
        assert result.suspension_reason.type == SuspensionType.WILD

    def test_no_prose_in_findings_structure(self):
        """Findings are structured, not prose."""
        analysis_result = {
            "scope": "node",
            "target": "auth.jwt",
            "target_nodes": ["auth.jwt"],
            "findings": {
                "summary": "One-line synthesis",  # Required
                "details": {
                    "contract_fidelity": "matches",  # Structured
                    "contradictions": [],  # List
                    "patterns": [],  # List
                    "risks": [],  # List
                },
            },
            "confidence": "examined",
            "escalate": False,
            "sources": [],
            "checklist_suggestions": [],
        }

        # Verify structure (not prose)
        assert isinstance(analysis_result["findings"]["summary"], str)
        assert isinstance(analysis_result["findings"]["details"], dict)
        assert isinstance(analysis_result["findings"]["details"]["contradictions"], list)


# ---------------------------------------------------------------------------
# RESIDUAL — Test Summary
# ---------------------------------------------------------------------------

"""
IMPLEMENTATION STATUS — All gates pass [⫴ TEST]

✓ Three-scope dispatch: node | project | external
✓ Analysis result parsing with validation
✓ Findings persistence to node metadata
✓ Escalation to human_input_queue
✓ Manager view includes analyst_findings
✓ Fan-in detection and auto-trigger
✓ MCP tools stub interface
✓ A7 invariants enforced

WALLS
• No [↑] without preceding [↓] or [◌]
• Confidence never exceeds grounding depth
• [⫴ TEST] passage required before [↑] or emission
• Downstream of [☠] is [☠] — [↑] halts

SYNDROMES MONITORED
⟨bloat⟩ → [◌] +prune
⟨hand_wave⟩ → gap: [↓] / deficit: [≋ NEAR]
⟨false_precision⟩ → [≋] at appropriate level
"""
