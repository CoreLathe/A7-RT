"""
Extracted inline tests for editor.py.
Run standalone: python tests/test_editor.py
"""

import os
import sys

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
from pathlib import Path

from a7_rt_core.tools.editor import EditError, Editor


def _test_editor() -> None:
    """
    Self-contained test covering all op types, diff, stale_check,
    out-of-bounds rejection, and batch atomicity.
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

    INITIAL = "line one\nline two\nline three\nline four\nline five\n"

    with tempfile.TemporaryDirectory() as tmpdir:
        content_dir = Path(tmpdir)
        ed = Editor(content_dir)

        # ── write / read ──────────────────────────────────────────────────────
        ed.write("node-a", INITIAL)
        check("write + read roundtrip", ed.read("node-a") == INITIAL)

        # ── display (line numbers) ────────────────────────────────────────────
        disp = ed.display("node-a")
        check("display: line 1 present", "1\tline one" in disp)
        check("display: line 5 present", "5\tline five" in disp)
        check("display: no line 6", "6\t" not in disp)

        # ── content_hash / stale_check ────────────────────────────────────────
        h = ed.content_hash("node-a")
        check("stale_check: fresh → False", not ed.stale_check("node-a", h))

        ed.write("node-a", INITIAL + "line six\n")
        check("stale_check: modified → True", ed.stale_check("node-a", h))

        # Reset to INITIAL
        ed.write("node-a", INITIAL)

        # ── patch: replace ────────────────────────────────────────────────────
        result = ed.patch(
            "node-a",
            [
                {
                    "type": "replace",
                    "start": 2,
                    "end": 3,
                    "lines": ["REPLACED TWO", "REPLACED THREE"],
                }
            ],
        )
        lines = result.splitlines()
        check("replace: line 1 unchanged", lines[0] == "line one")
        check("replace: line 2 replaced", lines[1] == "REPLACED TWO")
        check("replace: line 3 replaced", lines[2] == "REPLACED THREE")
        check("replace: line 4 unchanged", lines[3] == "line four")
        check("replace: line count preserved", len(lines) == 5)
        # File not yet written
        check("replace: original file untouched", ed.read("node-a") == INITIAL)
        # Commit and verify
        ed.commit("node-a", result)
        check("replace: commit persisted", ed.read("node-a") == result)

        # Reset
        ed.write("node-a", INITIAL)

        # ── patch: insert at top (after=0) ────────────────────────────────────
        result = ed.patch(
            "node-a", [{"type": "insert", "after": 0, "lines": ["line zero"]}]
        )
        lines = result.splitlines()
        check("insert top: new first line", lines[0] == "line zero")
        check("insert top: original first line", lines[1] == "line one")
        check("insert top: 6 lines total", len(lines) == 6)

        # ── patch: insert in middle ───────────────────────────────────────────
        result = ed.patch(
            "node-a", [{"type": "insert", "after": 2, "lines": ["inserted after two"]}]
        )
        lines = result.splitlines()
        check("insert middle: line 2 unchanged", lines[1] == "line two")
        check("insert middle: inserted line", lines[2] == "inserted after two")
        check("insert middle: line 3 shifted", lines[3] == "line three")
        check("insert middle: 6 lines total", len(lines) == 6)

        # ── patch: insert at end ──────────────────────────────────────────────
        result = ed.patch(
            "node-a", [{"type": "insert", "after": 5, "lines": ["line six"]}]
        )
        lines = result.splitlines()
        check("insert end: last line appended", lines[-1] == "line six")
        check("insert end: 6 lines total", len(lines) == 6)

        # ── patch: delete ─────────────────────────────────────────────────────
        result = ed.patch("node-a", [{"type": "delete", "start": 2, "end": 4}])
        lines = result.splitlines()
        check("delete: line 1 remains", lines[0] == "line one")
        check("delete: line 5 remains", lines[1] == "line five")
        check("delete: 2 lines remain", len(lines) == 2)

        # ── patch: regex first ────────────────────────────────────────────────
        result = ed.patch(
            "node-a",
            [
                {
                    "type": "regex",
                    "pattern": r"line",
                    "replacement": "LINE",
                    "scope": "first",
                }
            ],
        )
        check("regex first: first occurrence replaced", result.startswith("LINE one"))
        check("regex first: second occurrence unchanged", "line two" in result)

        # ── patch: regex global ───────────────────────────────────────────────
        result = ed.patch(
            "node-a",
            [
                {
                    "type": "regex",
                    "pattern": r"line",
                    "replacement": "LINE",
                    "scope": "global",
                }
            ],
        )
        check("regex global: all occurrences replaced", "line" not in result)
        check("regex global: 5 replacements", result.count("LINE") == 5)

        # ── patch: multiple ops (replace + delete, non-overlapping) ──────────
        result = ed.patch(
            "node-a",
            [
                {"type": "replace", "start": 1, "end": 1, "lines": ["FIRST"]},
                {"type": "delete", "start": 4, "end": 5},
            ],
        )
        lines = result.splitlines()
        check("multi-op: replaced line 1", lines[0] == "FIRST")
        check("multi-op: lines 4-5 deleted", len(lines) == 3)
        check("multi-op: line 2 intact", lines[1] == "line two")

        # ── diff ──────────────────────────────────────────────────────────────
        d = ed.diff("node-a", "completely different\n")
        check("diff: non-empty for changed content", len(d) > 0)
        check("diff: unified format marker", "@@" in d)

        d_same = ed.diff("node-a", ed.read("node-a"))
        check("diff: empty for identical content", d_same == "")

        # ── out-of-bounds rejection ───────────────────────────────────────────
        expect_raise(
            "OOB: end > n",
            EditError,
            lambda: ed.patch(
                "node-a", [{"type": "replace", "start": 3, "end": 99, "lines": ["x"]}]
            ),
        )
        expect_raise(
            "OOB: start < 1",
            EditError,
            lambda: ed.patch("node-a", [{"type": "delete", "start": 0, "end": 2}]),
        )
        expect_raise(
            "OOB: start > end",
            EditError,
            lambda: ed.patch(
                "node-a", [{"type": "replace", "start": 3, "end": 1, "lines": ["x"]}]
            ),
        )
        expect_raise(
            "OOB: insert after > n",
            EditError,
            lambda: ed.patch(
                "node-a", [{"type": "insert", "after": 99, "lines": ["x"]}]
            ),
        )

        # ── backreference rejection ───────────────────────────────────────────
        expect_raise(
            "regex: backreference rejected",
            EditError,
            lambda: ed.patch(
                "node-a",
                [{"type": "regex", "pattern": r"(line)", "replacement": r"\1 copy"}],
            ),
        )

        # ── bad regex pattern rejected ────────────────────────────────────────
        expect_raise(
            "regex: invalid pattern rejected",
            EditError,
            lambda: ed.patch(
                "node-a",
                [{"type": "regex", "pattern": r"[unclosed", "replacement": "x"}],
            ),
        )

        # ── batch atomicity: bad op in list → nothing written ─────────────────
        original_content = ed.read("node-a")
        # Mix a valid op with an OOB op — nothing should be written
        expect_raise(
            "batch atomicity: raises on bad op in batch",
            EditError,
            lambda: ed.patch(
                "node-a",
                [
                    {"type": "replace", "start": 1, "end": 1, "lines": ["VALID"]},
                    {"type": "delete", "start": 1, "end": 999},  # OOB
                ],
            ),
        )
        check(
            "batch atomicity: file unchanged after failed patch",
            ed.read("node-a") == original_content,
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\033[32mAll editor tests passed.\033[0m")


if __name__ == "__main__":
    print("Running editor tests...")
    _test_editor()
