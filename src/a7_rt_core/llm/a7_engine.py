"""
A7-RT A7 Engine — Analyst Oracle (CONSULT handler)

Read-only oracle used by the harness for two purposes:
  1. The manager's CONSULT action: contradicting interface contracts,
     architecture questions, high-propagation-risk decisions.
  2. The human's /reason command (passed through unchanged by harness).

The engine is NOT called by subagents. Subagents are stateless and isolated.
When a subagent returns escalate=true, the harness detects it; the manager
decides to CONSULT on its next turn; the harness calls A7Engine.query().
The subagent never knows the engine exists.

No state mutation. No master.json access. Pure query/response.

Imports:
  - LLMClient from llm_client    (transport layer)
  - SubagentError from models    (shared exception; defined there to avoid
                                  circular deps: models ← subagent ← harness)

Usage:
    from llm_client import LLMClient
    from a7_engine import A7Engine

    engine = A7Engine(llm, protocols_dir=Path("a7-rt-core/protocols"))
    verdict = engine.query(
        question="Is caching the right layer for this?",
        context=analyst_view_dict,
        constraints=["Must not require Redis", "Session must stay stateless"],
    )
    print(verdict.verdict)
    print(verdict.operations)   # ["[↑ use-memcached]", "[↺ retry-design]"]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel

from a7_rt_core.core.models import SubagentError
from a7_rt_core.llm.client import LLMClient, LLMError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A7Verdict
# ---------------------------------------------------------------------------


class A7Verdict(BaseModel):
    """
    Structured response from the A7 engine.

    Distinct from AnalystReturn (which is the subagent schema used by
    subagent.py for per-node dispatch). A7Verdict carries additional fields
    specific to the oracle role: operation tags and a confidence level.

    Fields
    ------
    verdict     : compressed answer (≤ 500 chars — the engine must compress)
    operations  : A7 operation tags extracted from the response,
                  e.g. ["[↑ use-redis]", "[↺ retry]", "[☠ cache-001]"]
    conflicts   : detected contradictions in the provided context, if any
    confidence  : epistemic confidence using NodeStatus vocabulary
                  ("grounded" | "provisional" | "suspended" | "void")
    raw_response: always preserved — forensic trail, never truncated
    """

    verdict: str
    operations: list[str]
    conflicts: Optional[list[str]] = None
    confidence: Literal["grounded", "provisional", "suspended", "void"]
    raw_response: str


# ---------------------------------------------------------------------------
# A7Engine
# ---------------------------------------------------------------------------


class A7Engine:
    """
    A7 analyst oracle. Wraps LLMClient with the full A7 protocol system prompt.

    Parameters
    ----------
    llm           : LLMClient instance (transport, with retry)
    protocols_dir : directory containing full.md (the complete Armature-7 protocol)
    max_tokens    : output token budget for the oracle (shorter than subagents —
                    the engine must compress into A7Verdict.verdict)
    """

    def __init__(
        self,
        llm: LLMClient,
        protocols_dir: Path,
        max_tokens: int = 2048,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens

        full_path = Path(protocols_dir) / "full.md"
        self._system_prompt = (
            full_path.read_text(encoding="utf-8") if full_path.exists() else ""
        )
        if not self._system_prompt:
            logger.warning(
                "protocols/full.md not found or empty — engine running without A7 protocol"
            )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def query(
        self,
        question: str,
        context: Optional[dict] = None,
        constraints: Optional[list[str]] = None,
    ) -> A7Verdict:
        """
        Call the A7 oracle with a question and optional context.

        Parameters
        ----------
        question    : the question for the engine to answer
        context     : analyst_view() dict from context.py (assembled by harness);
                      serialized to JSON in the user message
        constraints : hard constraints to respect in the answer (e.g.
                      "must not require Redis", "session must stay stateless")

        Returns
        -------
        A7Verdict on both success and malformed-response failure.
        On malformed LLM output: returns A7Verdict with confidence="void"
        and verdict describing the parse failure.

        Raises
        ------
        SubagentError : when LLMClient raises LLMError (API failure after retries)
        """
        user_msg = _build_user_message(question, context, constraints)

        try:
            # Tools are NEVER passed here — the A7 oracle has no external tools.
            raw = self._llm.call(
                system=self._system_prompt,
                user=user_msg,
                max_tokens=self._max_tokens,
            )
        except LLMError as exc:
            raise SubagentError(f"A7 engine LLM call failed: {exc}") from exc

        return _parse_verdict(raw)

    def as_hook(self) -> Any:
        """
        Return a callable matching the engine hook signature:
          (question, context, constraints) -> A7Verdict
        """

        def hook(
            question: str,
            context: Optional[dict] = None,
            constraints: Optional[list[str]] = None,
        ) -> A7Verdict:
            return self.query(question, context=context, constraints=constraints)

        return hook


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_message(
    question: str,
    context: Optional[dict],
    constraints: Optional[list[str]],
) -> str:
    """Assemble the user message: question + context + constraints."""
    parts: list[str] = [f"Question: {question}"]

    if context:
        parts.append("Context:\n" + json.dumps(context, indent=2, default=str))

    if constraints:
        constraint_block = "\n".join(f"- {c}" for c in constraints)
        parts.append(f"Constraints (must be respected):\n{constraint_block}")

    parts.append(
        "\nReturn a JSON object with fields:\n"
        "  verdict     (str): compressed answer\n"
        '  operations  (list[str]): A7 operation tags, e.g. ["[↑ use-redis]", "[↺ retry]"]\n'
        "  conflicts   (list[str] | null): detected contradictions in the context\n"
        '  confidence  (str): one of "grounded" | "provisional" | "suspended" | "void"'
    )

    return "\n\n".join(parts)


def _parse_verdict(raw: str) -> A7Verdict:
    """
    Parse the LLM response into an A7Verdict.

    On any parse error: returns A7Verdict with confidence="void" and a
    verdict describing the failure. The raw response is always preserved.
    """
    import re

    text = raw.strip()

    # Strip markdown fences
    if text.startswith("```"):
        inner: list[str] = []
        in_fence = False
        for line in text.splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                inner.append(line)
        text = "\n".join(inner).strip()

    # Try direct JSON parse, then brace-extract fallback
    data: Optional[dict] = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except json.JSONDecodeError:
        pass

    if data is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                pass

    if data is None:
        logger.error("A7 engine: no valid JSON in response: %s", raw[:300])
        return A7Verdict(
            verdict=f"Engine returned malformed response (no JSON): {raw[:200]}",
            operations=[],
            conflicts=None,
            confidence="void",
            raw_response=raw,
        )

    # Validate required fields
    verdict_text = data.get("verdict")
    if not verdict_text or not isinstance(verdict_text, str):
        return A7Verdict(
            verdict=f"Engine returned malformed response (missing verdict): {raw[:200]}",
            operations=[],
            conflicts=None,
            confidence="void",
            raw_response=raw,
        )

    # Parse confidence with fallback
    confidence_raw = data.get("confidence", "void")
    if confidence_raw not in ("grounded", "provisional", "suspended", "void"):
        logger.warning(
            "A7 engine: unknown confidence %r, defaulting to void", confidence_raw
        )
        confidence_raw = "void"

    # Extract operation tags (A7 bracketed tags anywhere in verdict or ops list)
    ops_from_data = data.get("operations") or []
    if not isinstance(ops_from_data, list):
        ops_from_data = []
    # Also scan the verdict text for any A7 tags not in the ops list
    ops_from_verdict = re.findall(r"\[[^\]]+\]", verdict_text)
    seen: set[str] = set(ops_from_data)
    combined_ops: list[str] = list(ops_from_data)
    for tag in ops_from_verdict:
        if tag not in seen:
            combined_ops.append(tag)
            seen.add(tag)

    conflicts = data.get("conflicts")
    if conflicts is not None and not isinstance(conflicts, list):
        conflicts = [str(conflicts)]

    return A7Verdict(
        verdict=verdict_text,
        operations=combined_ops,
        conflicts=conflicts or None,
        confidence=confidence_raw,  # type: ignore[arg-type]
        raw_response=raw,
    )
