"""
A7-RT Subagent Dispatch

LLM dispatch layer for each subagent role. The harness calls this via its
subagent_hook; this module handles role selection, protocol weight injection,
prompt assembly, API call with retry, return parsing, and normalization.

Role flow:
  builder      → BuilderReturn     → SubagentReturn
  test_author  → TestAuthorReturn  → SubagentReturn
  analyst      → AnalystReturn     → SubagentReturn

Malformed return → SubagentReturn(status=SUSPENDED, type=wild), raw output logged.
API persistent failure → raises SubagentError (harness marks suspended:far).
Context length exceeded → raises SubagentError("context_length") → harness HALTs.

Protocol weight injection:
  none  → no protocol block
  lean  → protocols/lean.md prepended to system prompt
  full  → protocols/full.md prepended to system prompt

Usage (as harness subagent_hook):
    from openai import OpenAI
    from llm_client import LLMClient
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key="sk-or-...")
    llm = LLMClient(client, model="anthropic/claude-haiku-4.5")
    agent = Subagent(llm, roles_dir=Path("a7-rt-core/roles"),
                     protocols_dir=Path("a7-rt-core/protocols"))
    harness = Harness(repo, manager_hook, agent.as_hook())
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from a7_rt_core.llm.client import LLMClient, LLMError

if TYPE_CHECKING:
    from a7_rt_core.agent.loop import AgentLoop
from a7_rt_core.core.models import (
    NodeStatus,
    ProtocolWeight,
    SubagentError,
    SubagentReturn,
    SuspensionReason,
    SuspensionType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Role-specific return schemas (raw parsed JSON before normalization)
# ---------------------------------------------------------------------------

# BuilderReturn fields expected from LLM JSON:
#   node_id, status, content, interface, dependencies, suspension_reason, escalate
#
# TestAuthorReturn fields:
#   node_id, test_script, contract_tested, assumptions
#
# AnalystReturn fields:
#   query, verdict, recommendations, conflicts


# ---------------------------------------------------------------------------
# Subagent
# ---------------------------------------------------------------------------


class Subagent:
    """
    LLM dispatch for each subagent role.

    Parameters
    ----------
    llm           : LLMClient instance (transport layer with retry)
    roles_dir     : directory containing builder.md, test_author.md, analyst.md
    protocols_dir : directory containing none.md, lean.md, full.md
    max_tokens    : maximum output tokens per call
    """

    def __init__(
        self,
        llm: LLMClient,
        roles_dir: Path,
        protocols_dir: Path,
        content_dir: Path | None = None,
        max_tokens: int = 4096,
        context_mode: str = "accumulate",
        on_raw_response: Any | None = None,
    ) -> None:
        self._llm = llm
        self._roles_dir = Path(roles_dir)
        self._protocols_dir = Path(protocols_dir)
        self._content_dir = content_dir
        self._max_tokens = 8192  # Increased from 4096 to reduce truncation
        self._context_mode = context_mode
        self._on_raw_response = on_raw_response

        # Cache file contents at construction time
        self._role_prompts: dict[str, str] = {}
        self._protocol_blocks: dict[str, str] = {}
        self._load_prompts()

        # Initialize agent loop if content_dir provided
        self._agent_loop: Optional["AgentLoop"] = None
        if content_dir:
            from a7_rt_core.agent.loop import AgentLoop

            self._agent_loop = AgentLoop(
                llm,
                str(content_dir),
                str(self._roles_dir),
                max_tokens,
                context_mode=self._context_mode,
            )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def dispatch(
        self,
        node_id: str,
        role: str,
        ctx: dict,
        protocol_weight: str = ProtocolWeight.LEAN.value,
        session_id: str | None = None,
        on_tool_call: Any | None = None,
        shadow: Any | None = None,
    ) -> SubagentReturn:
        """
        Dispatch a subagent call for the given role.

        Phase 3: Builder and test_author use AgentLoop for tool-enabled iteration.
        Analyst remains one-shot (no tools).

        Parameters
        ----------
        node_id         : the node being worked on
        role            : "builder" | "test_author" | "analyst"
        ctx             : context view from context.py
        protocol_weight : "none" | "lean" | "full"

        Returns
        -------
        SubagentReturn  : normalized result for the harness

        Raises
        ------
        SubagentError   : on persistent LLM API failure or context-length overflow
        """
        # Phase 3: Use AgentLoop for builder, test_author, and analyst roles
        if role in ("builder", "test_author", "analyst") and self._agent_loop is not None:
            return self._dispatch_with_loop(node_id, role, ctx, session_id, on_tool_call, shadow)

        # One-shot dispatch for analyst and legacy mode
        system = self._build_system_prompt(role, protocol_weight)
        user = self._build_user_message(role, node_id, ctx)

        raw = self._call_api(system, user)

        # Emit raw response if callback provided (for debugging)
        if self._on_raw_response:
            self._on_raw_response(node_id, role, {"raw_response": raw, "role": role})

        return self._parse_return(role, node_id, raw)

    def _dispatch_with_loop(
        self,
        node_id: str,
        role: str,
        ctx: dict,
        session_id: str | None = None,
        on_tool_call: Any | None = None,
        shadow: Any | None = None,
    ) -> SubagentReturn:
        """
        Dispatch using AgentLoop for tool-enabled iteration.

        Extracts expected_iterations from ctx and uses configured max_iterations.
        """
        # Extract expected iterations from ctx (manager guidance)
        manager_guidance = ctx.get("manager_guidance", {})
        expected_iterations = 5  # default
        if "expected_iterations" in manager_guidance:
            expected_iterations = manager_guidance["expected_iterations"]
        elif "target" in ctx and isinstance(ctx["target"], dict):
            # Check if stored in node metadata
            meta = ctx["target"].get("metadata", {})
            if meta and "avg_iterations" in meta:
                expected_iterations = int(meta["avg_iterations"]) or 5

        assert self._agent_loop is not None, (
            "AgentLoop must be initialized for tool-enabled dispatch"
        )
        result = self._agent_loop.run(
            node_id=node_id,
            role=role,
            ctx=ctx,
            expected_iterations=expected_iterations,
            checkpoint_interval=10,  # Telemetry checkpoint
            progress_threshold=3,  # Redispatch if no progress
            session_id=session_id,
            on_tool_call=on_tool_call,
            shadow=shadow,
        )
        # Emit raw response if callback provided (for debugging)
        if self._on_raw_response:
            self._on_raw_response(node_id, role, result)
        return result

    def as_hook(self) -> Any:
        """
        Return a callable matching SubagentHook signature:
          (node_id, role, ctx, session_id=None, on_tool_call=None) -> SubagentReturn

        Protocol weight is read from ctx["target"]["protocol_weight"] for
        builder/test_author, defaults to lean for analyst.
        """

        def hook(
            node_id: str,
            role: str,
            ctx: dict,
            session_id: str | None = None,
            on_tool_call: Any | None = None,
            shadow: Any | None = None,
        ) -> SubagentReturn:
            weight = _extract_weight(ctx)
            return self.dispatch(
                node_id,
                role,
                ctx,
                protocol_weight=weight,
                session_id=session_id,
                on_tool_call=on_tool_call,
                shadow=shadow,
            )

        return hook

    # -----------------------------------------------------------------------
    # Prompt assembly
    # -----------------------------------------------------------------------

    def _build_system_prompt(self, role: str, protocol_weight: str) -> str:
        """
        Assemble the system prompt: optional protocol block + role definition.

        Protocol weight injection:
          none → no protocol block prepended
          lean → protocols/lean.md prepended
          full → protocols/full.md prepended
        """
        parts: list[str] = []

        if protocol_weight in (ProtocolWeight.LEAN.value, ProtocolWeight.FULL.value):
            block = self._protocol_blocks.get(protocol_weight, "")
            if block:
                parts.append(block)

        role_prompt = self._role_prompts.get(role, "")
        if role_prompt:
            parts.append(role_prompt)

        return "\n\n---\n\n".join(parts) if parts else ""

    def _build_user_message(self, role: str, node_id: str, ctx: dict) -> str:
        """
        Serialize the context view as JSON and wrap it in a role-appropriate prompt.
        The LLM must return a JSON object matching the role's return schema.
        """
        ctx_json = json.dumps(ctx, indent=2, default=str)

        if role == "builder":
            return (
                f"Node to implement: {node_id}\n\n"
                f"Context:\n{ctx_json}\n\n"
                "Return a JSON object with fields: "
                "node_id, status (grounded|provisional|suspended|poisoned), "
                "content (the work product as a string), "
                "interface ({exports: [...], assumptions: [...]}), "
                "dependencies ([{from_node, to_node, type}]), "
                "suspension_reason ({type, detail} or null), "
                "escalate (bool)."
            )
        elif role == "test_author":
            return (
                f"Write tests for node: {node_id}\n\n"
                f"Context:\n{ctx_json}\n\n"
                "Return a JSON object with fields: "
                "node_id, test_script (executable test content as a string), "
                "contract_tested ([list of interface elements verified]), "
                "assumptions ([what the test assumes about inputs])."
            )
        elif role == "analyst":
            query = ctx.get("query", "Analyze the provided context.")
            return (
                f"Query: {query}\n\n"
                f"Context:\n{ctx_json}\n\n"
                "First, work through this analysis using A7 operations [◌ GROUND], [↓ DESCEND], [↑ ASCEND], [⫴ TEST], etc. "
                "Show your reasoning trace with explicit operation tags. Then return your final answer as JSON.\n\n"
                "Return a JSON object with the following structure:\n"
                "{\n"
                '  "status": "provisional" | "suspended",\n'
                '  "analysis_result": {\n'
                '    "scope": "node" | "project" | "external",\n'
                '    "target": "node_id or null",\n'
                '    "target_nodes": ["node_id", "..."],\n'
                '    "findings": {\n'
                '      "summary": "One-line synthesis (<120 chars)",\n'
                '      "details": {\n'
                '        "contract_fidelity": "matches|deviates|divergent|n/a",\n'
                '        "contradictions": ["list of contradictions"],\n'
                '        "patterns": ["patterns identified"],\n'
                '        "risks": ["security or integration risks"]\n'
                "      }\n"
                "    },\n"
                '    "confidence": "examined" | "inferred" | "sourced",\n'
                '    "escalate": false,\n'
                '    "sources": [\n'
                '      {"type": "file|web|assumption", "ref": "path or URL", "credibility": "high|medium|low"}\n'
                "    ],\n"
                '    "checklist_suggestions": [\n'
                '      {"text": "suggested manager action", "rationale": "why this matters"}\n'
                "    ]\n"
                "  },\n"
                '  "pr_note": "Synthesized summary for manager (<500 chars)",\n'
                '  "suspension_reason": {"type": "near|far|void|wild", "detail": "reason"}  // only if suspended\n'
                "}\n\n"
                "Scope rules:\n"
                "- node scope: single target node, examined confidence\n"
                "- project scope: multiple nodes, inferred confidence, checklist_suggestions allowed\n"
                "- external scope: empty target_nodes, sourced confidence, web/file sources required"
            )
        else:
            return f"Role '{role}' is unknown. Context:\n{ctx_json}"

    # -----------------------------------------------------------------------
    # API call with retry
    # -----------------------------------------------------------------------

    def _call_api(self, system: str, user: str) -> str:
        """
        Call the LLM via LLMClient (retry handled by LLMClient).

        On persistent failure: raises SubagentError.
        On context length exceeded: raises SubagentError("context_length").

        Returns the raw text content of the response.
        """
        try:
            return self._llm.call(system, user, max_tokens=self._max_tokens)
        except LLMError as exc:
            err_str = str(exc.cause).lower()
            if "context" in err_str and (
                "length" in err_str or "limit" in err_str or "too long" in err_str
            ):
                raise SubagentError("context_length") from exc
            raise SubagentError(f"LLM API unavailable: {exc}") from exc

    # -----------------------------------------------------------------------
    # Return parsing
    # -----------------------------------------------------------------------

    def _parse_return(self, role: str, node_id: str, raw: str) -> SubagentReturn:
        """
        Parse the raw LLM response string into a SubagentReturn.

        On any parse/schema error: returns SubagentReturn(status=SUSPENDED, type=wild)
        with the raw output logged. This matches the spec: malformed return →
        suspended:wild, raw output logged.
        """
        try:
            data = _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "Malformed return from %s subagent for node %s: %s\nRaw: %s",
                role,
                node_id,
                exc,
                raw[:500],
            )
            return _wild_suspension(f"Malformed JSON: {exc}")

        try:
            if role == "builder":
                return self._parse_builder(data, node_id, raw)
            elif role == "test_author":
                return self._parse_test_author(data, node_id, raw)
            elif role == "analyst":
                return self._parse_analyst(data, node_id)
            else:
                return _wild_suspension(f"Unknown role: {role}")
        except Exception as exc:
            logger.error(
                "Schema error in %s return for node %s: %s\nData: %s",
                role,
                node_id,
                exc,
                data,
            )
            return _wild_suspension(f"Schema error: {exc}")

    def _parse_builder(self, data: dict, node_id: str, raw: str) -> SubagentReturn:
        """
        Parse BuilderReturn schema → SubagentReturn.

        Supports both legacy JSON content field and new markdown fence format.
        Markdown fences take precedence if present.
        Extracts file_tags from JSON header for experiential annotations.
        """
        status_str = data.get("status", "suspended")
        try:
            status = NodeStatus(status_str)
        except ValueError:
            return _wild_suspension(f"Invalid builder status: {status_str!r}")

        # Parse markdown fences for multi-file support
        files = _parse_markdown_fences(raw)

        # Validate required fields for non-suspended returns
        has_content = "content" in data and data["content"]
        has_files = bool(files)
        if status == NodeStatus.GROUNDED and not has_content and not has_files:
            return _wild_suspension("Builder returned grounded without content or files")

        suspension_reason: Optional[SuspensionReason] = None
        if status == NodeStatus.SUSPENDED:
            sr = data.get("suspension_reason") or {}
            try:
                suspension_reason = SuspensionReason(
                    type=SuspensionType(sr.get("type", "near")),
                    detail=sr.get("detail", "No detail provided"),
                )
            except ValueError:
                suspension_reason = SuspensionReason(
                    type=SuspensionType.NEAR,
                    detail=str(sr),
                )

        iface = data.get("interface") or {}
        interface_update = (
            {
                "exports": iface.get("exports", []),
                "assumptions": iface.get("assumptions", []),
            }
            if iface
            else {}
        )

        new_deps = [
            {
                "from_node": dep.get("from_node", node_id),
                "to_node": dep.get("to_node", ""),
                "type": dep.get("type", "assumption"),
                "verified": False,
            }
            for dep in (data.get("dependencies") or [])
            if dep.get("to_node")
        ]

        # Extract file_tags for experiential annotations
        file_tags = [
            {
                "node_id": tag.get("node_id", ""),
                "category": tag.get("category", "quirk"),
                "content": tag.get("content", "")[:200],  # Enforce max length
                "propagate": tag.get("propagate", True),
            }
            for tag in (data.get("file_tags") or [])
            if tag.get("node_id") and tag.get("content")
        ]

        # Extract edits from ## Edits markdown section (Phase 2C)
        sections = _parse_markdown_sections(raw)
        edits: list[dict] = []
        if "edits" in sections:
            edits = _extract_edits_from_section(sections["edits"])

        # If markdown fences found, use files dict; otherwise fall back to content
        if files:
            return SubagentReturn(
                status=status,
                files=files,
                edits=edits,
                interface_update=interface_update,
                new_deps=new_deps,
                suspension_reason=suspension_reason,
                role="builder",
                escalate=bool(data.get("escalate", False)),
                file_tags=file_tags,
            )
        else:
            return SubagentReturn(
                status=status,
                content=data.get("content"),
                edits=edits,
                interface_update=interface_update,
                new_deps=new_deps,
                suspension_reason=suspension_reason,
                role="builder",
                escalate=bool(data.get("escalate", False)),
                file_tags=file_tags,
            )

    def _parse_test_author(self, data: dict, node_id: str, raw: str) -> SubagentReturn:
        """
        Parse TestAuthorReturn schema → SubagentReturn.

        The test script is stored as content. The harness writes it to the
        content_file. No status transition occurs — test_author returns are
        bookkeeping, not lifecycle changes. We use PROVISIONAL to indicate
        "test committed but not run yet"; the validator.py hard-path gate
        will transition to GROUNDED on pass.
        Extracts file_tags from JSON header for experiential annotations.
        """
        # Extract file_tags for experiential annotations
        file_tags = [
            {
                "node_id": tag.get("node_id", ""),
                "category": tag.get("category", "quirk"),
                "content": tag.get("content", "")[:200],  # Enforce max length
                "propagate": tag.get("propagate", True),
            }
            for tag in (data.get("file_tags") or [])
            if tag.get("node_id") and tag.get("content")
        ]

        # Extract edits from ## Edits markdown section (Phase 2C)
        sections = _parse_markdown_sections(raw)
        edits: list[dict] = []
        if "edits" in sections:
            edits = _extract_edits_from_section(sections["edits"])

        # Try markdown fences first for multi-file test support
        files = _parse_markdown_fences(raw)
        if files:
            return SubagentReturn(
                status=NodeStatus.PROVISIONAL,
                files=files,
                edits=edits,
                interface_update={},
                new_deps=[],
                suspension_reason=None,
                role="test_author",
                file_tags=file_tags,
            )

        # Legacy single-file path
        test_script = data.get("test_script")
        if not test_script:
            return _wild_suspension("TestAuthorReturn missing test_script")

        return SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            content=test_script,
            edits=edits,
            interface_update={},
            new_deps=[],
            suspension_reason=None,
            role="test_author",
            file_tags=file_tags,
        )

    def _parse_analyst(self, data: dict, node_id: str) -> SubagentReturn:
        """
        Parse AnalystReturn schema → SubagentReturn.

        Three-scope analyst: node | project | external
        Returns structured findings in analysis_result for harness routing.
        """
        # Support both new schema (analysis_result) and legacy schema
        analysis_result = data.get("analysis_result")

        if analysis_result:
            # New three-scope schema
            scope = analysis_result.get("scope")
            if not scope or scope not in ("node", "project", "external"):
                return _wild_suspension(f"AnalysisResult missing or invalid scope: {scope}")

            target_nodes = analysis_result.get("target_nodes", [])
            if scope == "node" and not target_nodes:
                return _wild_suspension("Node scope requires non-empty target_nodes")

            findings = analysis_result.get("findings")
            if not findings or not isinstance(findings, dict):
                return _wild_suspension("AnalysisResult missing findings dict")

            confidence = analysis_result.get("confidence")
            if confidence not in ("examined", "inferred", "sourced"):
                return _wild_suspension(f"Invalid confidence level: {confidence}")

            # Build analysis_result for harness routing
            parsed_analysis = {
                "scope": scope,
                "target": analysis_result.get("target"),
                "target_nodes": target_nodes,
                "findings": findings,
                "confidence": confidence,
                "escalate": analysis_result.get("escalate", False),
                "sources": analysis_result.get("sources", []),
                "checklist_suggestions": analysis_result.get("checklist_suggestions", []),
            }

            # Determine status based on findings and any suspension
            suspension = data.get("suspension_reason")
            status = NodeStatus.SUSPENDED if suspension else NodeStatus.PROVISIONAL

            return SubagentReturn(
                status=status,
                content=None,
                interface_update={},
                new_deps=[],
                suspension_reason=SuspensionReason(**suspension) if suspension else None,
                role="analyst",
                escalate=analysis_result.get("escalate", False),
                analysis_result=parsed_analysis,
                pr_note=data.get("pr_note"),
            )

        # Legacy schema fallback (deprecated)
        verdict = data.get("verdict")
        if not verdict:
            return _wild_suspension("AnalystReturn missing verdict and analysis_result")

        _ = json.dumps(
            {
                "query": data.get("query", ""),
                "verdict": verdict,
                "recommendations": data.get("recommendations", []),
                "conflicts": data.get("conflicts", []),
            },
            indent=2,
        )

        return SubagentReturn(
            status=NodeStatus.PROVISIONAL,
            content=None,
            interface_update={},
            new_deps=[],
            suspension_reason=None,
            role="analyst",
        )

    # -----------------------------------------------------------------------
    # File loading
    # -----------------------------------------------------------------------

    def _load_prompts(self) -> None:
        """Load role and protocol prompt files. Missing files become empty strings."""
        for role in ("builder", "test_author", "analyst"):
            path = self._roles_dir / f"{role}.md"
            self._role_prompts[role] = path.read_text(encoding="utf-8") if path.exists() else ""

        for weight in ("lean", "full"):
            path = self._protocols_dir / f"{weight}.md"
            self._protocol_blocks[weight] = (
                path.read_text(encoding="utf-8") if path.exists() else ""
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict:
    """
    Extract a JSON object from the raw LLM response string.

    LLMs often wrap JSON in markdown fences. This tries:
      1. Extract from ```json fences anywhere in text
      2. Direct json.loads on stripped text
      3. Extract content between first { and last }
    Raises ValueError if no valid JSON object found.
    """
    text = raw.strip()

    # Look for ```json block anywhere in text (not just at start)
    json_fence_start = text.find("```json")
    if json_fence_start != -1:
        # Find the closing ```
        fence_content_start = json_fence_start + 7  # len("```json")
        fence_end = text.find("```", fence_content_start)
        if fence_end != -1:
            json_text = text[fence_content_start:fence_end].strip()
            try:
                result = json.loads(json_text)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass  # Fall through to other methods

    # Strip markdown fences if text starts with ```
    if text.startswith("```"):
        lines = text.splitlines()
        inner = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                inner.append(line)
        text = "\n".join(inner).strip()

    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Fall back to extracting between first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON object found in response: {text[:200]!r}")


def _parse_markdown_fences(raw: str) -> dict[str, str]:
    """
    Parse markdown code fences from raw LLM response.

    Returns a dict of {path: content} for each fenced block with a path.
    Only recognizes fences with explicit paths like:
        ```python path/to/file.py
        content
        ```

    Returns empty dict if no valid fences found.
    """
    files: dict[str, str] = {}

    # Pattern matches:
    #   ```optional_lang path/to/file.ext
    #   content
    #   ```
    #
    # The path must contain a dot (extension) or forward slash
    fence_pattern = r"```(?:\w+)?\s+([^\s]+[\./][^\s]+)\n(.*?)```"

    for match in re.finditer(fence_pattern, raw, re.DOTALL):
        path = match.group(1).strip()
        content = match.group(2)

        # Clean up path (remove leading ./ or backticks)
        path = path.lstrip("./")
        path = path.strip("`")

        # Skip if path looks like a language tag (no dot or slash)
        if "." not in path and "/" not in path:
            continue

        # Normalize content (strip trailing whitespace, ensure single trailing newline)
        content = content.rstrip() + "\n"

        files[path] = content

    return files


def _wild_suspension(detail: str) -> SubagentReturn:
    """Return a wild suspension SubagentReturn with the given detail."""
    return SubagentReturn(
        status=NodeStatus.SUSPENDED,
        suspension_reason=SuspensionReason(
            type=SuspensionType.WILD,
            detail=detail,
        ),
    )


def _extract_weight(ctx: dict) -> str:
    """Read protocol_weight from the context view, defaulting to lean."""
    # Try target node first (builder/test_author)
    target = ctx.get("target") or {}
    weight = target.get("protocol_weight")

    # Fallback to analyst view's protocol_weight at root level
    if weight is None:
        weight = ctx.get("protocol_weight")

    # Default to lean if not found
    if weight is None:
        weight = ProtocolWeight.LEAN.value

    if weight in (ProtocolWeight.NONE.value, ProtocolWeight.LEAN.value, ProtocolWeight.FULL.value):
        return weight
    return ProtocolWeight.LEAN.value


def _parse_markdown_sections(raw: str) -> dict[str, str]:
    """
    Parse markdown sections (## Header) from raw LLM response.

    Fence-aware: content inside code fences (```) is NOT parsed as sections.
    This prevents collision when a subagent writes a file containing markdown headers.

    Returns a dict of {section_name: content} for each ## section found.
    Section name is normalized (lowercase, stripped).

    Example:
        Input: "## Summary\\nImplemented feature.\\n## Status\\ngrounded"
        Output: {"summary": "Implemented feature.", "status": "grounded"}
    """
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []
    in_fence = False

    for line in raw.splitlines():
        stripped = line.strip()

        # Track fence state
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        # Skip everything inside fences (don't accumulate to sections)
        if in_fence:
            continue

        # Only parse ## headers when outside fences
        if stripped.startswith("## "):
            # Save previous section if exists
            if current_section is not None:
                sections[current_section] = "\n".join(current_lines).strip()

            # Start new section
            current_section = stripped[3:].strip().lower()
            current_lines = []
        elif current_section is not None:
            # Accumulate content for current section
            current_lines.append(line)

    # Don't forget the last section
    if current_section is not None and current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


def _extract_edits_from_section(section_content: str) -> list[dict]:
    """
    Parse edit operations from an ## Edits section.

    Supports:
        REPLACE lines N-M in filename:
        INSERT after line N in filename:
        DELETE lines N-M in filename:

    Returns list of edit operation dicts ready for editor.py consumption.
    """
    edits: list[dict] = []
    lines = section_content.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Parse REPLACE lines N-M in filename:
        if match := re.match(r"REPLACE\s+lines\s+(\d+)-(\d+)\s+in\s+(\S+):", line, re.IGNORECASE):
            start, end, filename = (
                int(match.group(1)),
                int(match.group(2)),
                match.group(3),
            )
            # Collect fenced content
            content_lines = []
            i += 1
            if i < len(lines) and lines[i].strip().startswith("~~~"):
                i += 1  # Skip opening fence
                while i < len(lines) and not lines[i].strip().startswith("~~~"):
                    content_lines.append(lines[i])
                    i += 1
                i += 1  # Skip closing fence

            edits.append(
                {
                    "type": "replace",
                    "start": start,
                    "end": end,
                    "lines": content_lines,
                    "filename": filename,
                }
            )
            continue

        # Parse INSERT after line N in filename:
        if match := re.match(r"INSERT\s+after\s+line\s+(\d+)\s+in\s+(\S+):", line, re.IGNORECASE):
            after, filename = int(match.group(1)), match.group(2)
            content_lines = []
            i += 1
            if i < len(lines) and lines[i].strip().startswith("~~~"):
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("~~~"):
                    content_lines.append(lines[i])
                    i += 1
                i += 1

            edits.append(
                {
                    "type": "insert",
                    "after": after,
                    "lines": content_lines,
                    "filename": filename,
                }
            )
            continue

        # Parse DELETE lines N-M in filename:
        if match := re.match(r"DELETE\s+lines\s+(\d+)-(\d+)\s+in\s+(\S+):?", line, re.IGNORECASE):
            start, end, filename = (
                int(match.group(1)),
                int(match.group(2)),
                match.group(3),
            )
            edits.append(
                {
                    "type": "delete",
                    "start": start,
                    "end": end,
                    "filename": filename,
                }
            )
            i += 1
            continue

        i += 1

    return edits


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running subagent tests...")

    # Test _parse_markdown_fences with multi-file response
    raw_multi = """
## Summary
Implemented greet with tests.

```python greet.py
def greet(name):
    return f"Hello, {name}!"
```

```python greet_test.py
from greet import greet
assert greet("Alice") == "Hello, Alice!"
```
"""
    files = _parse_markdown_fences(raw_multi)
    assert "greet.py" in files, f"Expected greet.py in {list(files.keys())}"
    assert "greet_test.py" in files, f"Expected greet_test.py in {list(files.keys())}"
    assert "def greet" in files["greet.py"], "Expected def greet in greet.py content"
    assert "from greet import" in files["greet_test.py"], "Expected import in greet_test.py"
    print("  [OK] markdown fences: multi-file parsing")

    # Test _parse_markdown_fences returns empty for no fences
    raw_no_fences = '{"node_id": "n1", "status": "grounded", "content": "def foo(): pass"}'
    files = _parse_markdown_fences(raw_no_fences)
    assert files == {}, f"Expected empty dict, got {files}"
    print("  [OK] markdown fences: empty when no fences")

    # Test _parse_markdown_fences with language-only fence (no path)
    raw_lang_only = """
```json
{"status": "grounded"}
```
"""
    files = _parse_markdown_fences(raw_lang_only)
    assert files == {}, f"Expected empty dict for language-only fence, got {files}"
    print("  [OK] markdown fences: ignores language-only fences")

    # Test _parse_markdown_sections fence-aware parsing
    raw_with_sections = """
## Summary
Implemented feature.

```markdown README.md
# Project

## Edits
This is inside a fence - should be ignored.
```

## Status
grounded

## Interface
exports: ["foo()"]
"""
    sections = _parse_markdown_sections(raw_with_sections)
    assert "summary" in sections, f"Expected summary section, got {list(sections.keys())}"
    assert "status" in sections, f"Expected status section, got {list(sections.keys())}"
    assert "interface" in sections, f"Expected interface section, got {list(sections.keys())}"
    # The "## Edits" inside the fenced block should NOT create a section
    # It should be completely ignored
    assert "edits" not in sections, (
        f"'edits' section should not exist from fenced content, got {list(sections.keys())}"
    )
    print("  [OK] markdown sections: fence-aware parsing")

    # Test _parse_markdown_sections ignores content inside fences
    raw_collision_test = """
## Edits
REPLACE lines 1-2 in file.py:
~~~python
pass
~~~

```markdown README.md
## Edits
This is file content, not a control section.
```
"""
    sections = _parse_markdown_sections(raw_collision_test)
    edits_content = sections.get("edits", "")
    assert "This is file content" not in edits_content, "Fence content leaked into section"
    assert "REPLACE" in edits_content, "REPLACE directive should be in Edits section"
    print("  [OK] markdown sections: collision avoidance")

    # Test _extract_edits_from_section
    edits_section = """
REPLACE lines 1-2 in auth.py:
~~~python
def new():
    pass
~~~

INSERT after line 5 in utils.py:
~~~python
import logging
~~~

DELETE lines 10-12 in old.py:
"""
    edits = _extract_edits_from_section(edits_section)
    assert len(edits) == 3, f"Expected 3 edits, got {len(edits)}"
    assert edits[0]["type"] == "replace"
    assert edits[0]["start"] == 1
    assert edits[0]["end"] == 2
    assert edits[0]["filename"] == "auth.py"
    assert edits[1]["type"] == "insert"
    assert edits[1]["after"] == 5
    assert edits[2]["type"] == "delete"
    assert edits[2]["start"] == 10
    assert edits[2]["end"] == 12
    print("  [OK] edit extraction: all operation types")

    print("\nAll fence parsing tests passed.")
