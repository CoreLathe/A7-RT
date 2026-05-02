"""
Extracted inline tests for subagent.py.
Run standalone: python tests/test_subagent.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a7_rt_core.core.models import (
    NodeStatus,
    SubagentError,
    SubagentReturn,
    SuspensionType,
)
from a7_rt_core.llm.client import LLMError
from a7_rt_core.llm.subagent import Subagent


def _test_subagent() -> None:
    """
    Verify:
      1. Correct role prompt loaded
      2. Protocol weight injected (lean.md in system prompt)
      3. Valid BuilderReturn parsed → correct SubagentReturn
      4. Valid TestAuthorReturn parsed
      5. Valid AnalystReturn parsed
      6. Malformed return → suspended:wild
      7. API error → SubagentError (LLMClient raises LLMError)
      8. Context-length error → SubagentError("context_length")
    """
    ok = "\033[32mOK\033[0m"
    fail = "\033[31mFAIL\033[0m"
    errors: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            print(f"  [{ok}] {label}")
        else:
            print(f"  [{fail}] {label}")
            errors.append(label)

    def expect_raise(label: str, exc_type: type, fn) -> None:
        try:
            fn()
            print(f"  [{fail}] {label}  (no exception raised)")
            errors.append(label)
        except exc_type:
            print(f"  [{ok}] {label}")
        except Exception as e:
            print(f"  [{fail}] {label}  (wrong exception: {type(e).__name__}: {e})")
            errors.append(label)

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        roles_dir = root / "roles"
        roles_dir.mkdir()
        protocols_dir = root / "protocols"
        protocols_dir.mkdir()

        # Write stub prompt files
        (roles_dir / "builder.md").write_text(
            "# Builder Role\nYou implement nodes.", encoding="utf-8"
        )
        (roles_dir / "test_author.md").write_text(
            "# Test Author Role\nYou write tests.", encoding="utf-8"
        )
        (roles_dir / "analyst.md").write_text(
            "# Analyst Role\nYou analyse.", encoding="utf-8"
        )
        (protocols_dir / "lean.md").write_text(
            "# Lean Protocol\nFive operations.", encoding="utf-8"
        )
        (protocols_dir / "full.md").write_text(
            "# Full A7 Protocol\n1500 tokens.", encoding="utf-8"
        )

        # ── Stub LLMClient ────────────────────────────────────────────────────
        # LLMClient.call(system, user, max_tokens) → str  (or raises LLMError)

        class _StubLLMClient:
            """Stub LLMClient: records calls, returns canned text or raises."""

            def __init__(
                self, response_text: str = "", raises: Optional[Exception] = None
            ):
                self._text = response_text
                self._raises = raises
                self.calls: list[dict] = []

            def call(self, system: str, user: str, max_tokens: int = 4096) -> str:
                self.calls.append(
                    {"system": system, "user": user, "max_tokens": max_tokens}
                )
                if self._raises:
                    raise self._raises
                return self._text

        # ── Test 1: Role prompts loaded ───────────────────────────────────────

        llm1 = _StubLLMClient(
            json.dumps({"node_id": "n1", "status": "grounded", "content": "x"})
        )
        agent1 = Subagent(llm1, roles_dir, protocols_dir)

        check("role prompts loaded", bool(agent1._role_prompts.get("builder")))
        check("protocol blocks loaded", bool(agent1._protocol_blocks.get("lean")))

        # ── Test 2: Protocol weight injected ─────────────────────────────────

        ctx_builder = {
            "role": "builder",
            "target": {
                "node_id": "n1",
                "description": "x",
                "protocol_weight": "lean",
                "interface": {},
            },
            "structural_deps": {},
            "assumption_deps": {},
            "ancestor_interfaces": {},
            "ancestor_summaries": {},
            "tombstones": [],
        }

        agent1.dispatch("n1", "builder", ctx_builder, protocol_weight="lean")
        call = llm1.calls[0]
        sys_content = call["system"]
        check("lean protocol injected in system prompt", "Lean Protocol" in sys_content)
        check("builder role prompt injected", "Builder Role" in sys_content)

        llm_none = _StubLLMClient(
            json.dumps({"node_id": "n1", "status": "grounded", "content": "x"})
        )
        agent_none = Subagent(llm_none, roles_dir, protocols_dir)
        agent_none.dispatch("n1", "builder", ctx_builder, protocol_weight="none")
        sys_content_none = llm_none.calls[0]["system"]
        check(
            "no protocol block injected for weight=none",
            "Lean Protocol" not in sys_content_none
            and "Full A7 Protocol" not in sys_content_none,
        )

        # ── Test 3: Valid BuilderReturn ───────────────────────────────────────

        builder_json = json.dumps(
            {
                "node_id": "n1",
                "status": "grounded",
                "content": "def verify(): pass",
                "interface": {
                    "exports": ["verify()"],
                    "assumptions": ["jwt lib present"],
                },
                "dependencies": [
                    {"from_node": "n1", "to_node": "dep-001", "type": "assumption"}
                ],
                "suspension_reason": None,
                "escalate": False,
            }
        )
        llm2 = _StubLLMClient(builder_json)
        agent2 = Subagent(llm2, roles_dir, protocols_dir)
        ret = agent2.dispatch("n1", "builder", ctx_builder, protocol_weight="lean")

        check("builder: status=grounded", ret.status == NodeStatus.GROUNDED)
        check("builder: content set", ret.content == "def verify(): pass")
        check("builder: interface_update populated", "exports" in ret.interface_update)
        check("builder: new_dep discovered", len(ret.new_deps) == 1)
        check("builder: new_dep has to_node", ret.new_deps[0]["to_node"] == "dep-001")

        # ── Test 4: Suspended BuilderReturn ──────────────────────────────────

        suspended_json = json.dumps(
            {
                "node_id": "n1",
                "status": "suspended",
                "content": None,
                "interface": {},
                "dependencies": [],
                "suspension_reason": {"type": "far", "detail": "Need external schema"},
                "escalate": False,
            }
        )
        llm3 = _StubLLMClient(suspended_json)
        agent3 = Subagent(llm3, roles_dir, protocols_dir)
        ret3 = agent3.dispatch("n1", "builder", ctx_builder, protocol_weight="lean")

        check("builder: suspended status", ret3.status == NodeStatus.SUSPENDED)
        check("builder: suspension_reason set", ret3.suspension_reason is not None)
        check(
            "builder: suspension type=far",
            ret3.suspension_reason.type == SuspensionType.FAR,
        )

        # ── Test 5: Valid TestAuthorReturn ────────────────────────────────────

        ctx_ta = {
            "role": "test_author",
            "node_id": "n1",
            "description": "x",
            "interface": {},
            "structural_dep_interfaces": {},
            "assumption_dep_interfaces": {},
        }
        ta_json = json.dumps(
            {
                "node_id": "n1",
                "test_script": "assert verify_token('tok') is True",
                "contract_tested": ["verify_token"],
                "assumptions": ["valid token provided"],
            }
        )
        llm4 = _StubLLMClient(ta_json)
        agent4 = Subagent(llm4, roles_dir, protocols_dir)
        ret4 = agent4.dispatch("n1", "test_author", ctx_ta, protocol_weight="lean")

        check("test_author: status=provisional", ret4.status == NodeStatus.PROVISIONAL)
        check(
            "test_author: content=test_script",
            ret4.content == "assert verify_token('tok') is True",
        )

        # ── Test 6: Valid AnalystReturn ───────────────────────────────────────

        ctx_an = {
            "role": "analyst",
            "query": "Is node-glue correct?",
            "nodes": {},
            "dependencies": [],
            "tombstones": [],
        }
        an_json = json.dumps(
            {
                "query": "Is node-glue correct?",
                "verdict": "Yes, the wiring looks correct.",
                "recommendations": ["Add rate limit test"],
                "conflicts": [],
            }
        )
        llm5 = _StubLLMClient(an_json)
        agent5 = Subagent(llm5, roles_dir, protocols_dir)
        ret5 = agent5.dispatch("n1", "analyst", ctx_an, protocol_weight="lean")

        check("analyst: status=provisional", ret5.status == NodeStatus.PROVISIONAL)
        check("analyst: verdict in content", "verdict" in ret5.content)

        # ── Test 7: Malformed return → suspended:wild ─────────────────────────

        llm6 = _StubLLMClient("This is not JSON at all, just prose.")
        agent6 = Subagent(llm6, roles_dir, protocols_dir)
        ret6 = agent6.dispatch("n1", "builder", ctx_builder)

        check("malformed: status=suspended", ret6.status == NodeStatus.SUSPENDED)
        check(
            "malformed: type=wild", ret6.suspension_reason.type == SuspensionType.WILD
        )

        # Markdown-fenced JSON should parse correctly
        fenced = f"```json\n{builder_json}\n```"
        llm6b = _StubLLMClient(fenced)
        agent6b = Subagent(llm6b, roles_dir, protocols_dir)
        ret6b = agent6b.dispatch("n1", "builder", ctx_builder)
        check(
            "markdown-fenced JSON parsed correctly", ret6b.status == NodeStatus.GROUNDED
        )

        # ── Test 8: API error → SubagentError (LLMClient raises LLMError) ────

        class _APIError(Exception):
            pass

        # LLMClient raises LLMError after retries; stub raises it directly
        api_llm_error = LLMError(
            _APIError("rate limited"), attempt_count=3, model="test"
        )
        llm7 = _StubLLMClient(raises=api_llm_error)
        agent7 = Subagent(llm7, roles_dir, protocols_dir)

        expect_raise(
            "API error (LLMError) → SubagentError",
            SubagentError,
            lambda: agent7.dispatch("n1", "builder", ctx_builder),
        )

        # ── Test 9: Context length error → SubagentError("context_length") ───

        class _ContextError(Exception):
            pass

        ctx_llm_error = LLMError(
            _ContextError("prompt too long, context length exceeded"),
            attempt_count=1,
            model="test",
        )
        llm8 = _StubLLMClient(raises=ctx_llm_error)
        agent8 = Subagent(llm8, roles_dir, protocols_dir)

        try:
            agent8.dispatch("n1", "builder", ctx_builder)
            check("context_length: SubagentError raised", False)
        except SubagentError as e:
            check("context_length: SubagentError raised", True)
            check(
                "context_length: message is 'context_length'",
                "context_length" in str(e),
            )

        # ── Test 10: as_hook() ────────────────────────────────────────────────

        llm9 = _StubLLMClient(builder_json)
        agent9 = Subagent(llm9, roles_dir, protocols_dir)
        hook = agent9.as_hook()
        ret9 = hook("n1", "builder", ctx_builder)
        check("as_hook: returns SubagentReturn", isinstance(ret9, SubagentReturn))
        check("as_hook: weight extracted from ctx", ret9.status == NodeStatus.GROUNDED)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\033[32mAll subagent tests passed.\033[0m")


if __name__ == "__main__":
    print("Running subagent tests...")
    _test_subagent()
