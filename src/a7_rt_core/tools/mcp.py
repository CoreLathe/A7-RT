"""
MCP Tools — External Research Integration for Analyst Scope

Stub module for Model Context Protocol (MCP) tool integration.
When MCP framework is available, this module provides external research
capabilities for analyst external scope: web search, documentation fetch,
changelog parsing, security advisory lookup.

A7 Operation: [≋ NEAR/FAR] — External data required
Status: Interface defined, implementation pending MCP framework

Usage:
    from mcp_tools import MCPToolSet, research_external

    tools = MCPToolSet()
    result = await tools.web_search("OpenAPI 4.0 breaking changes")
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchResult:
    """
    Structured result from external research.
    Mirrors the analyst findings schema for direct integration.
    """

    query: str
    source_type: str  # "web" | "docs" | "changelog" | "advisory" | "none"
    content: str
    url: str | None = None
    credibility: str = "medium"  # "high" | "medium" | "low"
    timestamp: str | None = None

    def to_analyst_source(self) -> dict[str, Any]:
        """Convert to analyst findings sources format."""
        return {
            "type": self.source_type,
            "ref": self.url or self.query,
            "credibility": self.credibility,
        }


class MCPClient(Protocol):
    """
    Protocol for MCP client implementations.
    Implement this to connect to actual MCP servers.
    """

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool by name with arguments."""
        ...


# ---------------------------------------------------------------------------
# Tool Interface
# ---------------------------------------------------------------------------


class ExternalResearchTool(ABC):
    """
    Abstract base for external research tools.
    Concrete implementations connect to MCP servers or provide stubs.
    """

    name: str
    description: str

    @abstractmethod
    async def execute(self, query: str, **kwargs: Any) -> ResearchResult:
        """Execute the research query and return structured results."""
        pass


class WebSearchTool(ExternalResearchTool):
    """
    Web search for documentation, changelogs, best practices.
    [≋ FAR] — External data required
    """

    name = "web_search"
    description = "Search the web for documentation, changelogs, migration guides"

    async def execute(
        self, query: str, max_results: int = 5, **kwargs: Any
    ) -> ResearchResult:
        """
        [≋ VOID] — MCP framework not connected.

        When implemented: Searches web for query, returns top results
        with content snippets and source URLs.
        """
        # Stub: Return void result indicating need for implementation
        return ResearchResult(
            query=query,
            source_type="none",
            content="[≋ VOID] MCP web_search not implemented. "
            "Requires MCP framework integration.",
            credibility="low",
        )


class DocumentationFetchTool(ExternalResearchTool):
    """
    Fetch and parse specific documentation pages.
    [≋ FAR] — External data required
    """

    name = "fetch_docs"
    description = "Fetch and parse documentation from a specific URL"

    async def execute(
        self, query: str, url: str | None = None, **kwargs: Any
    ) -> ResearchResult:
        """
        [≋ VOID] — MCP framework not connected.

        When implemented: Fetches documentation from URL, parses markdown/HTML,
        extracts relevant sections based on query.
        """
        return ResearchResult(
            query=query,
            source_type="none",
            content="[≋ VOID] MCP fetch_docs not implemented. "
            "Requires MCP framework integration.",
            url=url,
            credibility="low",
        )


class ChangelogParseTool(ExternalResearchTool):
    """
    Parse changelogs for breaking changes, migration notes.
    [≋ FAR] — External data required
    """

    name = "parse_changelog"
    description = "Parse changelog for version differences and breaking changes"

    async def execute(
        self,
        query: str,
        package: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        **kwargs: Any,
    ) -> ResearchResult:
        """
        [≋ VOID] — MCP framework not connected.

        When implemented: Fetches changelog for package, extracts
        entries between versions, identifies breaking changes.
        """
        return ResearchResult(
            query=query,
            source_type="none",
            content="[≋ VOID] MCP parse_changelog not implemented. "
            "Requires MCP framework integration.",
            credibility="low",
        )


class SecurityAdvisoryTool(ExternalResearchTool):
    """
    Lookup security advisories by CVE, package, or pattern.
    [≋ FAR] — External data required
    """

    name = "security_advisory"
    description = "Lookup security advisories and CVE details"

    async def execute(
        self, query: str, cve_id: str | None = None, **kwargs: Any
    ) -> ResearchResult:
        """
        [≋ VOID] — MCP framework not connected.

        When implemented: Queries security databases (OSV, NVD, GitHub Advisories)
        for CVE details, affected versions, and mitigations.
        """
        return ResearchResult(
            query=query,
            source_type="none",
            content="[≋ VOID] MCP security_advisory not implemented. "
            "Requires MCP framework integration.",
            credibility="low",
        )


# ---------------------------------------------------------------------------
# Tool Set
# ---------------------------------------------------------------------------


class MCPToolSet:
    """
    Collection of MCP tools for external analyst scope.

    Usage:
        tools = MCPToolSet()
        result = await tools.research("OpenAPI 4.0", tools=["web_search"])
    """

    def __init__(self, client: MCPClient | None = None):
        self._client = client
        self._tools: dict[str, ExternalResearchTool] = {
            "web_search": WebSearchTool(),
            "fetch_docs": DocumentationFetchTool(),
            "parse_changelog": ChangelogParseTool(),
            "security_advisory": SecurityAdvisoryTool(),
        }

    def list_tools(self) -> list[dict[str, str]]:
        """Return available tool names and descriptions."""
        return [
            {"name": t.name, "description": t.description}
            for t in self._tools.values()
        ]

    async def execute(
        self, tool_name: str, query: str, **kwargs: Any
    ) -> ResearchResult:
        """Execute a specific tool by name."""
        tool = self._tools.get(tool_name)
        if not tool:
            return ResearchResult(
                query=query,
                source_type="none",
                content=f"[≋ VOID] Unknown tool: {tool_name}. "
                f"Available: {list(self._tools.keys())}",
                credibility="low",
            )
        return await tool.execute(query, **kwargs)

    async def research(
        self,
        query: str,
        tools: list[str] | None = None,
        **kwargs: Any,
    ) -> list[ResearchResult]:
        """
        Run research across multiple tools.

        If tools not specified, uses web_search as default.
        Returns results from all tools (including void results for unimplemented).
        """
        if tools is None:
            tools = ["web_search"]

        results: list[ResearchResult] = []
        for tool_name in tools:
            result = await self.execute(tool_name, query, **kwargs)
            results.append(result)

        return results

    def is_implemented(self) -> bool:
        """Check if any tools have actual MCP implementation."""
        return self._client is not None


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


async def research_external(
    query: str,
    scope: str = "external",
    tools: list[str] | None = None,
) -> dict[str, Any]:
    """
    Convenience function for analyst external scope research.

    Returns structured findings matching analyst schema:
        - findings.summary
        - sources[]
        - confidence: "sourced"

    Example:
        result = await research_external("OpenAPI 4.0 breaking changes")
    """
    mcp = MCPToolSet()
    results = await mcp.research(query, tools)

    # Aggregate results
    sources = [r.to_analyst_source() for r in results if r.source_type != "none"]

    if not sources:
        # No MCP implementation available
        return {
            "scope": scope,
            "findings": {
                "summary": f"[≋ VOID] External research not available: {query[:50]}...",
                "details": {
                    "contract_fidelity": "n/a",
                    "contradictions": [],
                    "patterns": ["MCP framework not connected"],
                    "risks": ["Cannot verify external information"],
                },
            },
            "confidence": "sourced",
            "sources": [],
            "escalate": False,
        }

    # Aggregate content from all results
    contents = [r.content for r in results if r.content]

    return {
        "scope": scope,
        "findings": {
            "summary": f"External research: {query[:60]}",
            "details": {
                "contract_fidelity": "n/a",
                "contradictions": [],
                "patterns": [],
                "risks": [],
                "research_results": contents,
            },
        },
        "confidence": "sourced",
        "sources": sources,
        "escalate": False,
    }


# ---------------------------------------------------------------------------
# Integration Hook
# ---------------------------------------------------------------------------


async def maybe_run_external_analyst(
    dispatch_action: Any,
) -> dict[str, Any] | None:
    """
    Hook for harness to auto-trigger external analyst research.

    Called when analyst scope is "external" and query is provided.
    Returns findings dict or None if MCP not available.

    This function is the integration point — called by harness
    before dispatching to the LLM subagent.
    """
    # Check if this is an external scope dispatch
    scope = getattr(dispatch_action, "scope", None)
    query = getattr(dispatch_action, "query", None)

    if scope != "external" or not query:
        return None

    # Run external research
    return await research_external(query)


# ---------------------------------------------------------------------------
# A7 Invariants
# ---------------------------------------------------------------------------

"""
INVARIANTS — Violations trigger [HALT] or [≋ VOID]

• External scope without query → [≋ VOID] — cannot ground research
• MCP client error → [≋ VOID] — degraded to sourced with low credibility
• No results found → valid [≋ NEAR/FAR] — escalate to human
• Unverified external data → confidence never exceeds "sourced"
• Network timeout → [≋ WILD] — external source unavailable

WALLS

• No compact encoding substitutes for required source attribution
• No confidence claims from LIMINAL until [⫴ TEST] passage
• [≋ NEAR] on formal objects requires [⬚ RECON]; escalate to VOID if unverifiable

MONITOR — ⟨syndromes⟩

⟨hand_wave⟩ → gap: external data assumed without fetch
⟨drift⟩ → [◌] +prune: adding interpretation beyond source content
⟨false_precision⟩ → [≋] at appropriate level: claiming examined from sourced
"""
