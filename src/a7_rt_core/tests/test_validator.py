"""
Extracted inline tests for validator.py.
Run standalone: python tests/test_validator.py
"""

import os
import sys

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a7_rt_core.core.models import NodeStatus as _NS
from a7_rt_core.core.models import SubagentReturn, SuspensionReason, SuspensionType
from a7_rt_core.validation.schema import Validator


def _test_validator() -> None:
    """
    Verify:
      1.  schema check passes on valid BuilderReturn (grounded)
      2.  schema check passes on valid BuilderReturn (suspended)
      3.  schema check passes on valid TestAuthorReturn
      4.  schema check passes on valid AnalystReturn
      5.  schema check fails on missing required field
      6.  schema check fails on null required field
      7.  schema check fails on invalid status value
      8.  schema check fails on suspended without suspension_reason
      9.  schema check fails on suspended with invalid suspension_reason.type
      10. schema check fails on grounded without interface.exports
      11. schema check fails on unknown role
      12. hard-path: passing test script → (True, "PASS")
      13. hard-path: failing test script → (False, detail with exit code)
      14. hard-path: syntax error → (False, detail with traceback)
      15. hard-path: timeout → (False, "timeout after Ns")
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

    v = Validator()

    # ── Schema checks ─────────────────────────────────────────────────────────

    print("\nSchema validation:")

    # 1. Valid grounded BuilderReturn
    valid_grounded = {
        "node_id": "n1",
        "status": "grounded",
        "content": "def verify(): pass",
        "interface": {"exports": ["verify()"], "assumptions": []},
        "dependencies": [],
        "suspension_reason": None,
        "escalate": False,
    }
    ok_flag, err = v.validate_schema("builder", valid_grounded)
    check("valid grounded BuilderReturn → pass", ok_flag and err == "")

    # 2. Valid suspended BuilderReturn
    valid_suspended = {
        "node_id": "n1",
        "status": "suspended",
        "content": None,
        "interface": {},
        "dependencies": [],
        "suspension_reason": {"type": "far", "detail": "Needs external schema"},
        "escalate": False,
    }
    ok_flag, err = v.validate_schema("builder", valid_suspended)
    check("valid suspended BuilderReturn → pass", ok_flag and err == "")

    # 3. Valid TestAuthorReturn
    valid_ta = {
        "node_id": "n1",
        "test_script": "assert 1 == 1",
        "contract_tested": ["verify_token"],
        "assumptions": [],
    }
    ok_flag, err = v.validate_schema("test_author", valid_ta)
    check("valid TestAuthorReturn → pass", ok_flag and err == "")

    # 4. Valid AnalystReturn
    valid_an = {
        "query": "Is the wiring correct?",
        "verdict": "Yes, looks correct.",
        "recommendations": [],
        "conflicts": [],
    }
    ok_flag, err = v.validate_schema("analyst", valid_an)
    check("valid AnalystReturn → pass", ok_flag and err == "")

    # 5. Missing required field (node_id missing)
    missing_field = {"status": "grounded", "content": "x"}  # no 'node_id'
    ok_flag, err = v.validate_schema("builder", missing_field)
    check("missing 'node_id' → fail", not ok_flag and "node_id" in err)

    # 6. Null required field (status is null)
    null_field = {"node_id": "n1", "status": None, "content": "x"}
    ok_flag, err = v.validate_schema("builder", null_field)
    check("null 'status' → fail", not ok_flag and ("null" in err or "status" in err))

    # 7. Invalid status value
    bad_status = {
        "node_id": "n1",
        "status": "flying",
        "content": "x",
        "interface": {"exports": ["f()"], "assumptions": []},
    }
    ok_flag, err = v.validate_schema("builder", bad_status)
    check("invalid status 'flying' → fail", not ok_flag and "flying" in err)

    # 8. Suspended without suspension_reason
    suspended_no_reason = {
        "node_id": "n1",
        "status": "suspended",
        "content": None,
        "suspension_reason": None,
    }
    ok_flag, err = v.validate_schema("builder", suspended_no_reason)
    check(
        "suspended without suspension_reason → fail",
        not ok_flag and "suspension_reason" in err,
    )

    # 9. Suspended with invalid suspension_reason.type
    bad_sr_type = {
        "node_id": "n1",
        "status": "suspended",
        "content": None,
        "suspension_reason": {"type": "maybe", "detail": "dunno"},
    }
    ok_flag, err = v.validate_schema("builder", bad_sr_type)
    check("invalid suspension_reason.type → fail", not ok_flag and "maybe" in err)

    # 10a. Grounded without interface field
    grounded_no_iface = {
        "node_id": "n1",
        "status": "grounded",
        "content": "x",
    }
    ok_flag, err = v.validate_schema("builder", grounded_no_iface)
    check("grounded without interface → fail", not ok_flag and "interface" in err)

    # 10b. Grounded with empty exports
    grounded_empty_exports = {
        "node_id": "n1",
        "status": "grounded",
        "content": "x",
        "interface": {"exports": [], "assumptions": []},
    }
    ok_flag, err = v.validate_schema("builder", grounded_empty_exports)
    check("grounded with empty exports → fail", not ok_flag and "exports" in err)

    # 11. Unknown role
    ok_flag, err = v.validate_schema("wizard", {"node_id": "n1"})
    check("unknown role → fail", not ok_flag and "wizard" in err)

    # ── Hard-path validation ──────────────────────────────────────────────────

    print("\nHard-path validation:")

    # 12. Passing test script
    passing_script = "assert 1 + 1 == 2\nassert 'hello'.upper() == 'HELLO'\n"
    passed, detail = v.run_hard_path(passing_script, timeout=15)
    check("passing test → (True, 'PASS')", passed and detail == "PASS")

    # 13. Failing test script (assertion error)
    failing_script = "assert 1 + 1 == 3, 'arithmetic is broken'\n"
    passed, detail = v.run_hard_path(failing_script, timeout=15)
    check("failing test → (False, detail)", not passed and "exit code" in detail)
    check(
        "failing test detail contains stderr",
        "AssertionError" in detail or "arithmetic" in detail,
    )

    # 14. Syntax error in script
    syntax_error_script = "def broken(\n    # unclosed parenthesis\n"
    passed, detail = v.run_hard_path(syntax_error_script, timeout=15)
    check("syntax error → (False, detail)", not passed)
    check("syntax error detail non-empty", len(detail) > 5)

    # 15. Timeout
    # Sleep well beyond our short timeout so this reliably fires
    timeout_script = "import time\ntime.sleep(60)\n"
    passed, detail = v.run_hard_path(timeout_script, timeout=2)
    check(
        "timeout → (False, 'timeout after 2s')",
        not passed and "timeout after 2s" in detail,
    )

    # ── validate_return ───────────────────────────────────────────────────────

    print("\nvalidate_return (SubagentReturn → validate_schema):")

    # 16. Valid grounded builder SubagentReturn
    sr_grounded = SubagentReturn(
        status=_NS.GROUNDED,
        content="def verify(): pass",
        interface_update={"exports": ["verify()"], "assumptions": []},
        role="builder",
    )
    ok_flag, err = v.validate_return(sr_grounded, "n1")
    check("validate_return: grounded builder → pass", ok_flag and err == "")

    # 17. Valid suspended builder SubagentReturn
    sr_suspended = SubagentReturn(
        status=_NS.SUSPENDED,
        content=None,
        suspension_reason=SuspensionReason(
            type=SuspensionType.FAR, detail="needs schema"
        ),
        role="builder",
    )
    ok_flag, err = v.validate_return(sr_suspended, "n1")
    check("validate_return: suspended builder → pass", ok_flag and err == "")

    # 18. Grounded builder missing exports → fail
    sr_no_exports = SubagentReturn(
        status=_NS.GROUNDED,
        content="def f(): pass",
        interface_update={"exports": [], "assumptions": []},
        role="builder",
    )
    ok_flag, err = v.validate_return(sr_no_exports, "n1")
    check(
        "validate_return: grounded missing exports → fail",
        not ok_flag and "exports" in err,
    )

    # 19. Suspended builder missing suspension_reason → fail
    sr_sus_no_reason = SubagentReturn(
        status=_NS.SUSPENDED,
        content=None,
        suspension_reason=None,
        role="builder",
    )
    ok_flag, err = v.validate_return(sr_sus_no_reason, "n1")
    check(
        "validate_return: suspended missing reason → fail",
        not ok_flag and "suspension_reason" in err,
    )

    # 20. Valid test_author SubagentReturn
    sr_ta = SubagentReturn(
        status=_NS.PROVISIONAL,
        content="assert 1 == 1",
        role="test_author",
    )
    ok_flag, err = v.validate_return(sr_ta, "n1")
    check("validate_return: test_author → pass", ok_flag and err == "")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        total = 20  # count of check() calls above
        print(f"\033[32mAll {total} validator tests passed.\033[0m")


if __name__ == "__main__":
    print("Running validator tests...")
    _test_validator()
