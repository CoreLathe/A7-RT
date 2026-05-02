"""
A7-RT Editor

Atomic line-addressed file edit operations for subagent work-product files.
Applies to the content/ directory managed by Repository.

Operations (matching A7-RT.md spec):
  replace(start, end, lines)       — replace lines[start..end] (1-indexed, inclusive)
  insert(after, lines)             — insert after line N; after=0 prepends
  delete(start, end)               — delete lines[start..end] (1-indexed, inclusive)
  regex(pattern, replacement, scope) — re.sub; scope "first"|"global"; no backreferences

Batch atomicity:
  patch() validates ALL ops against the current file before applying any.
  On validation failure, EditError is raised and nothing is written.
  Ops execute: line ops first (in reverse line order, so offsets stay valid),
  then regex ops on the resulting text.
  Caller receives the result text; call commit() to persist atomically.

Stale detection:
  content_hash() — sha256 of current file content.
  stale_check(node_id, expected_hash) → True if the file has changed since
  expected_hash was recorded (i.e. the subagent's line numbers are stale).

Display:
  display() returns content with 1-indexed line numbers for LLM context,
  matching the format expected by all line-addressed operations.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Literal, Optional

try:
    from typing import TypedDict, Required
except ImportError:
    from typing_extensions import TypedDict, Required  # type: ignore


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class EditOp(TypedDict, total=False):
    """
    One edit operation.  Required fields depend on type:

      replace: type, start, end, lines
      insert:  type, after, lines
      delete:  type, start, end
      regex:   type, pattern, replacement, scope
    """
    type: Required[Literal["replace", "insert", "delete", "regex"]]
    start:       int           # 1-indexed inclusive (replace, delete)
    end:         int           # 1-indexed inclusive (replace, delete)
    after:       int           # 1-indexed; 0 = prepend (insert)
    lines:       list[str]     # replacement/insertion content (replace, insert)
    pattern:     str           # regex pattern (regex)
    replacement: str           # replacement string, no backreferences (regex)
    scope:       Literal["first", "global"]  # (regex); default "first"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EditError(Exception):
    """Raised when an edit operation is invalid. No writes occur on EditError."""


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------

class Editor:
    """
    Manages line-addressed edits over a flat content directory.

    All write paths use write-to-temp + os.replace() — atomic on POSIX.
    """

    def __init__(self, content_dir: Path) -> None:
        self.content_dir = Path(content_dir)

    # -----------------------------------------------------------------------
    # Read / display
    # -----------------------------------------------------------------------

    def _path(self, node_id: str) -> Path:
        return self.content_dir / node_id

    def read(self, node_id: str) -> str:
        """Return raw file content."""
        return self._path(node_id).read_text(encoding="utf-8")

    def display(self, node_id: str) -> str:
        """
        Return content with 1-indexed line numbers for LLM context.

        Format matches cat -n style:
          1\tline one
          2\tline two
        """
        lines = self.read(node_id).splitlines()
        width = len(str(max(len(lines), 1)))
        return "\n".join(f"{i + 1:{width}d}\t{line}" for i, line in enumerate(lines))

    # -----------------------------------------------------------------------
    # Hashing / stale detection
    # -----------------------------------------------------------------------

    def content_hash(self, node_id: str) -> str:
        """SHA-256 of current content. Record at read time; pass to stale_check()."""
        return hashlib.sha256(self.read(node_id).encode("utf-8")).hexdigest()

    def stale_check(self, node_id: str, expected_hash: str) -> bool:
        """
        Return True if the file has changed since expected_hash was recorded.

        A True result means the subagent's line numbers are stale — it must
        re-read (display()) before issuing line-addressed operations.
        """
        return self.content_hash(node_id) != expected_hash

    # -----------------------------------------------------------------------
    # Atomic write
    # -----------------------------------------------------------------------

    def write(self, node_id: str, text: str) -> None:
        """Atomic full overwrite of node content."""
        self._atomic_write(self._path(node_id), text)

    def commit(self, node_id: str, text: str) -> None:
        """Persist the result of patch() atomically."""
        self._atomic_write(self._path(node_id), text)

    # -----------------------------------------------------------------------
    # Diff
    # -----------------------------------------------------------------------

    def diff(self, node_id: str, new_text: str) -> str:
        """
        Return a unified diff between current content and new_text.
        Used by the harness to record every edit in the event log.
        Returns empty string if content is identical.
        """
        old_lines = self.read(node_id).splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"{node_id} (before)",
            tofile=f"{node_id} (after)",
        ))

    # -----------------------------------------------------------------------
    # Patch
    # -----------------------------------------------------------------------

    def patch(self, node_id: str, ops: list[EditOp]) -> str:
        """
        Apply a batch of edit operations to the current file content.

        Returns the resulting text without writing. Call commit() to persist.

        Atomicity contract:
          All ops are validated against the ORIGINAL content before any are
          applied. If any op is invalid (out-of-bounds, bad regex), EditError
          is raised and nothing changes.

        Application order:
          1. Line ops (replace/insert/delete) applied in REVERSE line order so
             that each op's original line numbers remain valid throughout.
          2. Regex ops applied in declaration order on the post-line-op text.
        """
        text = self.read(node_id)
        lines = _split_lines(text)
        n = len(lines)

        line_ops  = [op for op in ops if op["type"] != "regex"]
        regex_ops = [op for op in ops if op["type"] == "regex"]

        # -- Validate all ops before touching anything -----------------------

        for op in line_ops:
            t = op["type"]
            if t in ("replace", "delete"):
                s = op.get("start")
                e = op.get("end")
                if s is None or e is None:
                    raise EditError(f"{t}: 'start' and 'end' are required")
                if s < 1:
                    raise EditError(f"{t}: start={s} is less than 1")
                if e > n:
                    raise EditError(
                        f"{t}: end={e} exceeds file length ({n} lines)"
                    )
                if s > e:
                    raise EditError(f"{t}: start={s} > end={e}")
            elif t == "insert":
                after = op.get("after")
                if after is None:
                    raise EditError("insert: 'after' is required")
                if after < 0 or after > n:
                    raise EditError(
                        f"insert: after={after} out of range [0, {n}]"
                    )

        for op in regex_ops:
            if not op.get("pattern"):
                raise EditError("regex: 'pattern' is required")
            if "replacement" not in op:
                raise EditError("regex: 'replacement' is required")
            # Validate pattern compiles
            try:
                re.compile(op["pattern"])
            except re.error as exc:
                raise EditError(f"regex: invalid pattern {op['pattern']!r}: {exc}") from exc
            # No backreferences allowed (spec constraint)
            if re.search(r"\\[1-9]", op.get("replacement", "")):
                raise EditError(
                    f"regex: replacement {op['replacement']!r} contains "
                    "backreferences — not permitted"
                )

        # -- Apply line ops in reverse line order ----------------------------

        def _sort_key(op: EditOp) -> int:
            t = op["type"]
            if t == "insert":
                return op.get("after", 0)
            return op.get("start", 0)

        for op in sorted(line_ops, key=_sort_key, reverse=True):
            t = op["type"]
            if t == "replace":
                s, e = op["start"] - 1, op["end"]
                new = [l.rstrip("\n") for l in op.get("lines", [])]
                lines[s:e] = new
            elif t == "insert":
                idx = op["after"]
                new = [l.rstrip("\n") for l in op.get("lines", [])]
                lines[idx:idx] = new
            elif t == "delete":
                s, e = op["start"] - 1, op["end"]
                del lines[s:e]

        # -- Apply regex ops in declaration order on resulting text ----------

        result_text = _join_lines(lines)
        for op in regex_ops:
            count = 0 if op.get("scope", "first") == "global" else 1
            result_text = re.sub(op["pattern"], op["replacement"], result_text, count=count)

        return result_text

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _atomic_write(self, target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Internal text helpers
# ---------------------------------------------------------------------------

def _split_lines(text: str) -> list[str]:
    """Split preserving content; no trailing newline artifact."""
    return text.splitlines()


def _join_lines(lines: list[str]) -> str:
    """Rejoin lines, always ending with a single newline."""
    return "\n".join(lines) + "\n" if lines else ""


