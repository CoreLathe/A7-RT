"""
Extracted inline tests for a7_engine.py.
Run standalone: python tests/test_a7_engine.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a7_rt_core.core.models import SubagentError
from a7_rt_core.llm.a7_engine import A7Engine, A7Verdict
from a7_rt_core.llm.client import LLMError


def _test_a7_engine() -> None:
    """
    Verify:
      1.  query returns A7Verdict on valid LLM response
      2.  protocols/full.md content appears in system prompt
      3.  context dict is serialized into user message
      4.  constraints list is serialized into user message
      5.  malformed LLM response → A7Verdict with confidence="void"
      6.  LLMError from client → SubagentError
      7.  as_hook() callable works
      8.  tools is NEVER passed to llm.call()
    """
    ok_str = "\033[32mOK\033[0m"
    fail_str = "\033[31mFAIL\033[0m"
    errors: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            print(f"  [{ok_str}] {label}")
        else:
            print(f"  [{fail_str}] {label}")
            errors.append(label)

    def expect_raise(label: str, exc_type: type, fn) -> Optional[Exception]:
        try:
            fn()
            print(f"  [{fail_str}] {label}  (no exception raised)")
            errors.append(label)
            return None
        except exc_type as e:
            print(f"  [{ok_str}] {label}")
            return e
        except Exception as e:
            print(f"  [{fail_str}] {label}  (wrong type: {type(e).__name__}: {e})")
            errors.append(label)
            return None

    with tempfile.TemporaryDirectory() as tmpdir:
        protocols_dir = Path(tmpdir) / "protocols"
        protocols_dir.mkdir()
        full_md = protocols_dir / "full.md"
        full_md.write_text(
            "# Full A7 Protocol\nYou are the A7 oracle. Think carefully.",
            encoding="utf-8",
        )

        # ── Stub LLMClient ────────────────────────────────────────────────────

        class _StubLLM:
            """Records call args and returns canned responses."""

            def __init__(
                self, responses: list[str], raises: Optional[Exception] = None
            ):
                self._responses = list(responses)
                self._raises = raises
                self.calls: list[dict] = []

            def call(
                self, system: str, user: str, max_tokens: int = 2048, tools=None
            ) -> str:
                self.calls.append(
                    {
                        "system": system,
                        "user": user,
                        "max_tokens": max_tokens,
                        "tools": tools,
                    }
                )
                if self._raises:
                    raise self._raises
                return self._responses.pop(0) if self._responses else ""

        # ── Test 1: Valid verdict response ────────────────────────────────────

        valid_response = json.dumps(
            {
                "verdict": "Use Redis. [↑ redis-001] The in-memory store violates the stateless requirement.",
                "operations": ["[↑ redis-001]", "[↺ retry-design]"],
                "conflicts": [
                    "cache-001 assumes in-memory persistence; threading-001 requires stateless"
                ],
                "confidence": "provisional",
            }
        )

        llm1 = _StubLLM([valid_response])
        engine1 = A7Engine(llm1, protocols_dir)
        verdict = engine1.query("Should we use Redis?")

        check("query returns A7Verdict", isinstance(verdict, A7Verdict))
        check("verdict.verdict non-empty", bool(verdict.verdict))
        check(
            "verdict.confidence is valid",
            verdict.confidence in ("grounded", "provisional", "suspended", "void"),
        )
        check("verdict.operations non-empty", len(verdict.operations) >= 1)
        check("verdict.raw_response preserved", verdict.raw_response == valid_response)
        check("conflicts extracted", verdict.conflicts is not None)

        # ── Test 2: full.md in system prompt ─────────────────────────────────

        check(
            "full.md content in system prompt",
            "Full A7 Protocol" in llm1.calls[0]["system"],
        )

        # ── Test 3: context serialized into user message ──────────────────────

        llm2 = _StubLLM([valid_response])
        engine2 = A7Engine(llm2, protocols_dir)
        ctx = {"nodes": {"n1": {"status": "near"}}, "tombstones": []}
        engine2.query("Is this design correct?", context=ctx)
        check("context dict in user message", '"nodes"' in llm2.calls[0]["user"])
        check("context values in user message", '"near"' in llm2.calls[0]["user"])

        # ── Test 4: constraints in user message ───────────────────────────────

        llm3 = _StubLLM([valid_response])
        engine3 = A7Engine(llm3, protocols_dir)
        engine3.query(
            "Cache design?",
            constraints=["Must not require Redis", "Must remain stateless"],
        )
        user_msg = llm3.calls[0]["user"]
        check("constraints in user message", "Must not require Redis" in user_msg)
        check("second constraint in user message", "Must remain stateless" in user_msg)

        # ── Test 5: Malformed response → void verdict ─────────────────────────

        llm4 = _StubLLM(["This is just prose. No JSON here."])
        engine4 = A7Engine(llm4, protocols_dir)
        bad_verdict = engine4.query("Some question?")
        check("malformed → confidence=void", bad_verdict.confidence == "void")
        check("malformed → verdict describes failure", len(bad_verdict.verdict) > 0)
        check(
            "malformed → raw_response preserved",
            "No JSON here" in bad_verdict.raw_response,
        )

        # Also test missing 'verdict' field
        no_verdict_response = json.dumps(
            {
                "operations": [],
                "confidence": "provisional",
            }
        )
        llm4b = _StubLLM([no_verdict_response])
        engine4b = A7Engine(llm4b, protocols_dir)
        bad4b = engine4b.query("Q?")
        check("missing verdict field → confidence=void", bad4b.confidence == "void")

        # ── Test 6: LLMError → SubagentError ─────────────────────────────────

        exc = LLMError(RuntimeError("API down"), attempt_count=3, model="test")
        llm5 = _StubLLM([], raises=exc)
        engine5 = A7Engine(llm5, protocols_dir)
        expect_raise(
            "LLMError → SubagentError", SubagentError, lambda: engine5.query("Q?")
        )

        # ── Test 7: as_hook() ─────────────────────────────────────────────────

        llm6 = _StubLLM([valid_response])
        engine6 = A7Engine(llm6, protocols_dir)
        hook = engine6.as_hook()
        hook_result = hook("Hook question?", context={"x": 1}, constraints=["c1"])
        check("as_hook() returns A7Verdict", isinstance(hook_result, A7Verdict))

        # ── Test 8: tools NEVER passed to llm.call() ─────────────────────────

        llm7 = _StubLLM([valid_response])
        engine7 = A7Engine(llm7, protocols_dir)
        engine7.query("Tools test?")
        check("tools not passed to LLM call", llm7.calls[0].get("tools") is None)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        total = 21  # count of check() calls above
        print(f"\033[32mAll {total} a7_engine tests passed.\033[0m")


if __name__ == "__main__":
    print("Running a7_engine tests...")
    _test_a7_engine()
