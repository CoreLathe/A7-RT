"""
A7-RT Agent Tools

Read-only file operations for the looping agent. All tools are scoped to
content_dir — attempts to read outside this directory raise ToolError.

Tools:
  - read_file(path) -> str
  - grep_content(pattern) -> list[dict]
  - list_files(glob) -> list[str]

All paths are relative to content_dir. Absolute paths are rejected.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from a7_rt_core.storage.shadowfs import ShadowFS, ShadowFSError
from a7_rt_core.validation.exports import validate_shadow_exports


class ToolError(Exception):
    """Raised when a tool operation fails (path outside scope, file not found, etc.)."""

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        self.message = message
        super().__init__(f"[{tool}] {message}")


def _format_with_line_numbers(content: str, start_line: int = 1) -> str:
    """
    Return content with 1-indexed line numbers for LLM context.

    Format includes SHA256 hash header for stale detection:
      # content_hash: sha256:abc123...
      1	line one
      2	line two

    Args:
        content: The file content to format
        start_line: Starting line number (default 1 for full file reads)

    Returns:
        Content with hash header and line numbers prepended
    """
    # Compute SHA256 hash for stale detection
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    hash_header = f"# content_hash: sha256:{content_hash}"

    lines = content.splitlines()
    width = len(str(start_line + len(lines) - 1))
    numbered_lines = "\n".join(f"{start_line + i:{width}d}\t{line}" for i, line in enumerate(lines))

    return f"{hash_header}\n{numbered_lines}"


def extract_hash(formatted_content: str) -> str | None:
    """
    Extract SHA256 hash from formatted content header.

    Parses first line for '# content_hash: sha256:...' pattern.

    Args:
        formatted_content: Content from read_file/preview_file with hash header

    Returns:
        Hash string or None if not found
    """
    lines = formatted_content.splitlines()
    if not lines:
        return None

    first_line = lines[0].strip()
    prefix = "# content_hash: sha256:"
    if first_line.startswith(prefix):
        return first_line[len(prefix) :]

    return None


class AgentTools:
    """
    ShadowFS-enabled file tools for the agent loop.

    When shadow is attached, all operations use the transactional layer:
    - Reads see shadow state (including agent's own writes)
    - Writes go to shadow only (no live disk changes)
    - Tests execute against staged shadow state

    Usage:
        tools = AgentTools(Path("/session/content"))
        tools.attach_shadow(shadow, node_id)  # Enable shadow mode

        # All operations now use shadow
        content = tools.read_file("auth/handler.py")
        tools.write_file("new.py", "content")  # Shadow only
        tools.run_test()  # Executes against staged shadow
    """

    def __init__(self, content_dir: str | Path) -> None:
        self._content_dir = Path(content_dir).resolve()
        self._shadow: ShadowFS | None = None
        self._node_id: str | None = None
        self._test_runs: list[dict] = []
        self._test_count = 0
        self._thoughts: list[dict] = []
        self._last_test_result: dict[str, Any] | None = None  # Phase 3
        self._usage: dict[str, int] = {
            "read_file": 0,
            "write_file": 0,
            "delete_file": 0,
            "rename_file": 0,
            "edit_file": 0,
            "edit_files": 0,
            "run_lint": 0,
            "grep_content": 0,
            "list_files": 0,
            "create_directory": 0,
            "preview_file": 0,
            "record_thought": 0,
            "run_test": 0,
            "validate_exports": 0,
            "get_current_state": 0,
        }

    # -------------------------------------------------------------------------
    # ShadowFS attachment
    # -------------------------------------------------------------------------

    def attach_shadow(self, shadow: ShadowFS, node_id: str) -> None:
        """
        Attach a ShadowFS instance for transactional operations.

        Args:
            shadow: ShadowFS instance for this dispatch
            node_id: The node being worked on (for test naming)
        """
        self._shadow = shadow
        self._node_id = node_id
        self._test_runs = []
        self._test_count = 0

    def detach_shadow(self) -> None:
        """Detach shadow and reset to live-file mode."""
        self._shadow = None
        self._node_id = None
        self._test_runs = []
        self._test_count = 0

    def get_shadow_writes(self) -> dict[str, str]:
        """
        Return files written to shadow layer.

        Returns:
            Dict mapping relative paths to content strings.
            Empty dict if shadow not attached or no writes.
        """
        if not self._shadow:
            return {}
        return self._shadow.get_writes()

    # -------------------------------------------------------------------------
    # Tool: read_file
    # -------------------------------------------------------------------------

    def read_file(self, path: str) -> str:
        """
        Read the contents of a file within content_dir.

        If shadow is attached, reads from shadow state (including own writes).
        Otherwise reads from live filesystem.

        Args:
            path: Relative path from content_dir (e.g., "auth/handler.py")

        Returns:
            File content as string with line numbers

        Raises:
            ToolError: If path escapes content_dir or file doesn't exist
        """
        self._usage["read_file"] += 1

        # Block reading test files - builders must NOT read *.test files
        # Test contracts are the specification, not the test implementation
        if path.endswith(".test"):
            raise ToolError(
                "read_file",
                f"Cannot read test file: {path}. Test files are implementation details. "
                "Use test_contract from context for specification.",
            )

        # Shadow mode: use ShadowFS
        if self._shadow:
            try:
                content = self._shadow.read(path)
                return _format_with_line_numbers(content)
            except FileNotFoundError as e:
                raise ToolError("read_file", str(e)) from e
            except ShadowFSError as e:
                raise ToolError("read_file", str(e)) from e

        # Live mode: direct filesystem access
        target = self._resolve_path(path)

        if not target.exists():
            raise ToolError("read_file", f"File not found: {path}")

        if not target.is_file():
            raise ToolError("read_file", f"Not a file: {path}")

        try:
            content = target.read_text(encoding="utf-8")
            return _format_with_line_numbers(content)
        except UnicodeDecodeError as e:
            raise ToolError("read_file", f"Cannot read binary file: {path}") from e
        except Exception as e:
            raise ToolError("read_file", f"Read error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: write_file
    # -------------------------------------------------------------------------

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        """
        Write file content to shadow layer.

        Requires shadow to be attached. Changes are not persisted to live
        disk until submit_pr is called.

        Args:
            path: Relative path from content_dir
            content: File content to write

        Returns:
            Dict confirming the write

        Raises:
            ToolError: If shadow not attached or path invalid
        """
        self._usage["write_file"] += 1

        if not self._shadow:
            raise ToolError("write_file", "ShadowFS not attached - cannot write files")

        try:
            self._shadow.write(path, content)
            return {
                "written": True,
                "path": path,
                "size": len(content),
            }
        except ShadowFSError as e:
            raise ToolError("write_file", str(e)) from e
        except Exception as e:
            raise ToolError("write_file", f"Write error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: delete_file
    # -------------------------------------------------------------------------

    def delete_file(self, path: str) -> dict[str, Any]:
        """
        Delete a file in shadow layer.

        Requires shadow to be attached. The file is marked as deleted
        but not actually removed from live disk until commit.

        Args:
            path: Relative path from content_dir

        Returns:
            Dict confirming the deletion

        Raises:
            ToolError: If shadow not attached or path invalid
        """
        self._usage["delete_file"] += 1

        if not self._shadow:
            raise ToolError("delete_file", "ShadowFS not attached - cannot delete files")

        try:
            self._shadow.delete(path)
            return {
                "deleted": True,
                "path": path,
            }
        except ShadowFSError as e:
            raise ToolError("delete_file", str(e)) from e
        except Exception as e:
            raise ToolError("delete_file", f"Delete error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: rename_file
    # -------------------------------------------------------------------------

    def rename_file(self, old_path: str, new_path: str) -> dict[str, Any]:
        """
        Rename a file in shadow layer.

        Requires shadow to be attached. The rename is staged but not
        applied to live disk until commit.

        Args:
            old_path: Original relative path
            new_path: New relative path

        Returns:
            Dict confirming the rename

        Raises:
            ToolError: If shadow not attached or paths invalid
        """
        self._usage["rename_file"] += 1

        if not self._shadow:
            raise ToolError("rename_file", "ShadowFS not attached - cannot rename files")

        try:
            self._shadow.rename(old_path, new_path)
            return {
                "renamed": True,
                "old_path": old_path,
                "new_path": new_path,
            }
        except ShadowFSError as e:
            raise ToolError("rename_file", str(e)) from e
        except Exception as e:
            raise ToolError("rename_file", f"Rename error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: run_test
    # -------------------------------------------------------------------------

    def run_test(self) -> dict[str, Any]:
        """
        Run the test file for current node against shadow state.

        Stages shadow to temp directory and executes {node_id}.test.
        Records test run for auto-commit validation.

        Returns:
            Dict with passed, output, exit_code, attempt_number

        Raises:
            ToolError: If shadow not attached or no test file
        """
        self._usage["run_test"] += 1

        if not self._shadow:
            raise ToolError("run_test", "ShadowFS not attached")

        if not self._node_id:
            raise ToolError("run_test", "node_id not set - attach_shadow not called")

        temp_dir = None
        try:
            # Stage shadow to temp directory
            temp_dir = self._shadow.stage_to_temp()
            test_path = temp_dir / f"{self._node_id}.test"

            if not test_path.exists():
                raise ToolError("run_test", f"No test file found at {self._node_id}.test")

            # Run the test
            result = subprocess.run(
                [sys.executable, str(test_path)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=temp_dir,
            )

            passed = result.returncode == 0
            self._test_count += 1
            self._record_test_run(passed, result.stdout, result.stderr, result.returncode)

            # Phase 3: Store for get_current_state()
            output = (result.stdout + "\n" + result.stderr).strip()
            self._last_test_result = {
                "passed": passed,
                "output": output[:2000] if len(output) > 2000 else output,  # Truncated for state
                "exit_code": result.returncode,
                "summary": "Test passed" if passed else "Test failed",
            }

            return {
                "passed": passed,
                "output": (result.stdout + "\n" + result.stderr).strip(),
                "exit_code": result.returncode,
                "attempt_number": self._test_count,
            }

        except subprocess.TimeoutExpired:
            self._test_count += 1
            self._record_test_run(False, "", "Test timed out after 30s", -1)
            raise ToolError("run_test", "Test execution timed out after 30 seconds")
        except ToolError:
            raise
        except Exception as e:
            raise ToolError("run_test", f"Test execution failed: {e}") from e
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _record_test_run(self, passed: bool, stdout: str, stderr: str, exit_code: int) -> None:
        """Record a test run for auto-commit validation."""
        from datetime import datetime, timezone

        output = (stdout + "\n" + stderr).strip()
        self._test_runs.append(
            {
                "passed": passed,
                "output": output[:2000],
                "exit_code": exit_code,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def get_test_runs(self) -> list[dict]:
        """Return recorded test runs for harness validation."""
        return list(self._test_runs)

    # -------------------------------------------------------------------------
    # Tool: create_directory
    # -------------------------------------------------------------------------

    def create_directory(self, path: str) -> dict[str, Any]:
        """
        Create a directory (and parents) in shadow layer.

        Establishes package structure before writing files. The directory
        is created in the staging area and commits with submit_pr.

        Args:
            path: Relative directory path from content_dir

        Returns:
            Dict confirming directory creation

        Raises:
            ToolError: If shadow not attached or path invalid
        """
        self._usage["create_directory"] += 1

        if not self._shadow:
            raise ToolError("create_directory", "ShadowFS not attached - cannot create directories")

        try:
            # ShadowFS doesn't track empty dirs, but we validate path
            # and ensure it will exist when files are written
            self._resolve_path(path)  # Validates path doesn't escape
            # Just validate - actual mkdir happens during staging/commit
            # when files are written to this directory
            return {
                "created": True,
                "path": path,
            }
        except ShadowFSError as e:
            raise ToolError("create_directory", str(e)) from e
        except Exception as e:
            raise ToolError("create_directory", f"Directory creation error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: preview_file
    # -------------------------------------------------------------------------

    def preview_file(
        self,
        path: str,
        offset: int = 0,
        max_lines: int = 50,
    ) -> dict[str, Any]:
        """
        Read partial content with absolute line numbers and content hash.

        Args:
            path: Relative path from content_dir (e.g., "auth/handler.py")
            offset: 0-indexed starting line (default 0)
            max_lines: Maximum lines to return, capped at 200 (default 50)

        Returns:
            Dict with path, offset, max_lines, total_lines, content_hash, and lines list

        Raises:
            ToolError: If path escapes content_dir or file doesn't exist
        """
        self._usage["preview_file"] += 1

        # Normalize and validate path
        target = self._resolve_path(path)

        if not target.exists():
            raise ToolError("preview_file", f"File not found: {path}")

        if not target.is_file():
            raise ToolError("preview_file", f"Not a file: {path}")

        # Clamp max_lines to reasonable range
        max_lines = max(1, min(max_lines, 200))

        try:
            # Shadow mode: use ShadowFS to see agent's own writes
            if self._shadow:
                try:
                    content = self._shadow.read(path)
                except FileNotFoundError:
                    raise ToolError("preview_file", f"File not found: {path}") from None
                except ShadowFSError as e:
                    raise ToolError("preview_file", str(e)) from e
            else:
                content = target.read_text(encoding="utf-8")

            lines = content.splitlines()
            total_lines = len(lines)

            # Compute content hash for stale detection (needed for all return paths)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            # Handle out-of-bounds offset
            if offset < 0:
                offset = 0
            if offset >= total_lines:
                return {
                    "path": path,
                    "offset": offset,
                    "max_lines": max_lines,
                    "total_lines": total_lines,
                    "content_hash": content_hash,
                    "lines": [],
                }

            # Slice lines and format with line numbers
            selected = lines[offset : offset + max_lines]
            result_lines = [
                {"line_number": offset + i + 1, "content": line} for i, line in enumerate(selected)
            ]

            return {
                "path": path,
                "offset": offset,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "content_hash": content_hash,
                "lines": result_lines,
            }

        except UnicodeDecodeError as e:
            raise ToolError("preview_file", f"Cannot read binary file: {path}") from e
        except Exception as e:
            raise ToolError("preview_file", f"Read error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: record_thought
    # -------------------------------------------------------------------------

    def record_thought(
        self,
        thought: str,
        category: Literal["hypothesis", "contradiction", "plan", "question"],
        relates_to: str | None = None,
    ) -> dict[str, Any]:
        """
        Capture structured reasoning for warm redispatch.

        Unlike ephemeral exploration, thoughts persist across dispatches
        to guide future agents and avoid redundant dead ends.

        Args:
            thought: The reasoning to record (max 500 chars)
            category: Type of reasoning (hypothesis, contradiction, plan, question)
            relates_to: Optional file path or node_id this relates to

        Returns:
            Dict confirming the thought was recorded
        """
        self._usage["record_thought"] += 1

        # Truncate if necessary
        thought = thought[:500]

        # Normalize empty string to None for relates_to
        if relates_to == "":
            relates_to = None

        # Generate timestamp
        timestamp = datetime.now(timezone.utc).isoformat()

        # Store for persistence across redispatches
        thought_record = {
            "thought": thought,
            "category": category,
            "relates_to": relates_to,
            "timestamp": timestamp,
        }
        self._thoughts.append(thought_record)

        return {
            "recorded": True,
            "thought": thought,
            "category": category,
            "relates_to": relates_to,
            "timestamp": timestamp,
        }

    def grep_content(self, pattern: str) -> list[dict[str, Any]]:
        """
        Search file contents for regex pattern within content_dir.

        If shadow is attached, searches shadow state; otherwise searches live files.

        Args:
            pattern: Regex pattern to search (Python regex syntax)

        Returns:
            List of match dicts: [{path, line_number, line_content, match, source, file_hash}]
            - file_hash: SHA256 of complete file for temporal anchor (use with edit_file)

        Raises:
            ToolError: If pattern is invalid regex
        """
        self._usage["grep_content"] += 1

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ToolError("grep_content", f"Invalid regex pattern: {e}") from e

        matches: list[dict[str, Any]] = []

        # Shadow mode: search shadow writes first, then live (excluding shadowed)
        if self._shadow:
            # Search shadow writes
            for path, content in self._shadow.get_writes().items():
                # Compute hash once per file for temporal anchor
                file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                lines = content.splitlines()
                for line_num, line in enumerate(lines, start=1):
                    match = compiled.search(line)
                    if match:
                        matches.append(
                            {
                                "path": path,
                                "line_number": line_num,
                                "line_content": line[:200],
                                "match": match.group(0)[:100],
                                "source": "shadow",
                                "file_hash": file_hash,
                            }
                        )

            # Search live files (excluding shadowed/deleted)
            excluded = set(self._shadow.get_writes().keys()) | self._shadow.get_deletions()
            for file_path in self._content_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                rel_path = file_path.relative_to(self._content_dir).as_posix()
                if rel_path in excluded:
                    continue
                if self._is_binary(file_path):
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8")
                    # Compute hash once per file for temporal anchor
                    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    lines = content.splitlines()
                    for line_num, line in enumerate(lines, start=1):
                        match = compiled.search(line)
                        if match:
                            rel_path = file_path.relative_to(self._content_dir).as_posix()
                            matches.append(
                                {
                                    "path": rel_path,
                                    "line_number": line_num,
                                    "line_content": line[:200],
                                    "match": match.group(0)[:100],
                                    "source": "live",
                                    "file_hash": file_hash,
                                }
                            )
                except (UnicodeDecodeError, Exception):
                    continue
        else:
            # Live mode: search all files
            for file_path in self._content_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                if self._is_binary(file_path):
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8")
                    # Compute hash once per file for temporal anchor
                    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    lines = content.splitlines()
                except (UnicodeDecodeError, Exception):
                    continue

                for line_num, line in enumerate(lines, start=1):
                    match = compiled.search(line)
                    if match:
                        rel_path = file_path.relative_to(self._content_dir).as_posix()
                        matches.append(
                            {
                                "path": rel_path,
                                "line_number": line_num,
                                "line_content": line[:200],
                                "match": match.group(0)[:100],
                                "source": "live",
                                "file_hash": file_hash,
                            }
                        )

        # Limit results to prevent context explosion
        return matches[:50]

    # -------------------------------------------------------------------------
    # Tool: edit_file (line-addressed editing)
    # -------------------------------------------------------------------------

    def edit_file(
        self,
        path: str,
        content_hash: str,
        operations: list[dict],
        intent: str | None = None,
    ) -> dict[str, Any]:
        """
        Apply line-addressed edits to a file.

        Operations are validated and applied atomically. All operations
        must pass validation before any are applied.

        Args:
            path: Relative path to edit
            content_hash: Expected SHA256 hash (from read_file/preview_file header)
            operations: List of edit operations:
                - {"type": "replace", "start": int, "end": int, "lines": [str]}
                - {"type": "insert", "after": int, "lines": [str]}
                - {"type": "delete", "start": int, "end": int}
            intent: Reason for edit (for chronicle)

        Returns:
            {"applied": True, "lines_changed": int, "new_hash": str}

        Raises:
            ToolError: Hash mismatch, invalid range, overlapping ranges
        """
        self._usage["edit_file"] = self._usage.get("edit_file", 0) + 1

        if not self._shadow:
            raise ToolError("edit_file", "ShadowFS not attached - cannot edit files")

        # Read current content from shadow
        try:
            current_content = self._shadow.read(path)
        except FileNotFoundError:
            raise ToolError("edit_file", f"File not found: {path}") from None
        except ShadowFSError as e:
            raise ToolError("edit_file", str(e)) from e

        # Verify hash
        current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
        # Normalize content_hash: strip sha256: prefix if present (from read_file header format)
        expected_hash = content_hash
        if expected_hash.startswith("sha256:"):
            expected_hash = expected_hash[7:]  # Remove "sha256:" prefix
        if current_hash != expected_hash:
            raise ToolError(
                "edit_file",
                f"Stale content: hash mismatch. Expected {expected_hash[:16]}..., "
                f"found {current_hash[:16]}...",
            )

        # Parse and validate operations
        lines = current_content.splitlines()
        total_lines = len(lines)
        line_ops = []

        for i, op in enumerate(operations):
            op_type = op.get("type")
            if op_type not in ("replace", "insert", "delete"):
                raise ToolError("edit_file", f"Operation {i}: invalid type '{op_type}'")

            if op_type in ("replace", "delete"):
                start = op.get("start")
                end = op.get("end")
                if start is None or end is None:
                    raise ToolError("edit_file", f"Operation {i}: 'start' and 'end' required")
                if start < 1:
                    raise ToolError("edit_file", f"Operation {i}: start={start} < 1")
                if end > total_lines:
                    raise ToolError(
                        "edit_file",
                        f"Operation {i}: end={end} exceeds file length ({total_lines})",
                    )
                if start > end:
                    raise ToolError(
                        "edit_file",
                        f"Operation {i}: start={start} > end={end}",
                    )
                line_ops.append((start, end, op_type, op))

            elif op_type == "insert":
                after = op.get("after")
                if after is None:
                    raise ToolError("edit_file", f"Operation {i}: 'after' required for insert")
                if after < 0 or after > total_lines:
                    raise ToolError(
                        "edit_file",
                        f"Operation {i}: after={after} out of range [0, {total_lines}]",
                    )
                line_ops.append((after, after, op_type, op))

        # Check for overlapping ranges
        sorted_ops = sorted(line_ops, key=lambda x: (x[0], x[1]))
        for i in range(len(sorted_ops) - 1):
            curr_start, curr_end, curr_type, _ = sorted_ops[i]
            next_start, next_end, next_type, _ = sorted_ops[i + 1]

            # Check overlap (insert at same position is ok, others overlap)
            if curr_type != "insert" or next_type != "insert":
                if curr_end >= next_start:
                    raise ToolError(
                        "edit_file",
                        f"Operations overlap: lines {curr_start}-{curr_end} and "
                        f"{next_start}-{next_end}",
                    )

        # Apply operations in reverse line order (so line numbers stay valid)
        new_lines = list(lines)
        for _, _, op_type, op in sorted(line_ops, key=lambda x: x[0], reverse=True):
            if op_type == "replace":
                start = op["start"] - 1  # Convert to 0-indexed
                end = op["end"]
                new_content = op.get("lines", [])
                new_lines[start:end] = new_content
            elif op_type == "delete":
                start = op["start"] - 1  # Convert to 0-indexed
                end = op["end"]
                del new_lines[start:end]
            elif op_type == "insert":
                after = op["after"]
                new_content = op.get("lines", [])
                new_lines[after:after] = new_content

        # Compute new content and hash
        new_content = "\n".join(new_lines)
        if new_lines:  # Add trailing newline if there was content
            new_content += "\n"
        new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()

        # Write to shadow
        self._shadow.write(path, new_content)

        # Count lines changed (approximate: sum of replaced/deleted/inserted)
        lines_changed = sum(
            len(op.get("lines", []))
            if op["type"] == "insert"
            else (op["end"] - op["start"] + 1)
            if op["type"] == "delete"
            else max(len(op.get("lines", [])), op["end"] - op["start"] + 1)
            for op in operations
        )

        return {
            "applied": True,
            "lines_changed": lines_changed,
            "new_hash": new_hash,
        }

    # -------------------------------------------------------------------------
    # Tool: edit_files (batch line-addressed editing)
    # -------------------------------------------------------------------------

    def edit_files(self, edits: list[dict]) -> dict[str, Any]:
        """
        Atomically apply edits to multiple files.

        All edits are validated before any are applied. If any edit fails
        validation, none are applied.

        Args:
            edits: List of edit specs:
                [{"path": str, "content_hash": str, "operations": [...]}]

        Returns:
            {"applied": [...], "failed": [...]}
        """
        self._usage["edit_files"] = self._usage.get("edit_files", 0) + 1

        if not self._shadow:
            raise ToolError("edit_files", "ShadowFS not attached - cannot edit files")

        # First pass: validate all edits
        validated = []
        for i, edit in enumerate(edits):
            path = edit.get("path")
            content_hash = edit.get("content_hash")
            operations = edit.get("operations", [])

            if not path:
                return {
                    "applied": [],
                    "failed": [{"index": i, "error": "Missing 'path'"}],
                }

            # Read and hash check
            try:
                current_content = self._shadow.read(path)
            except FileNotFoundError:
                return {
                    "applied": [],
                    "failed": [{"index": i, "path": path, "error": "File not found"}],
                }
            except ShadowFSError as e:
                return {
                    "applied": [],
                    "failed": [{"index": i, "path": path, "error": str(e)}],
                }

            current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
            # Normalize content_hash: strip sha256: prefix if present
            expected_hash = content_hash
            if isinstance(expected_hash, str) and expected_hash.startswith("sha256:"):
                expected_hash = expected_hash[7:]  # Remove "sha256:" prefix
            if current_hash != expected_hash:
                return {
                    "applied": [],
                    "failed": [
                        {
                            "index": i,
                            "path": path,
                            "error": f"Stale content: hash mismatch",
                            "expected": content_hash[:16] + "...",
                            "found": current_hash[:16] + "...",
                        }
                    ],
                }

            # Validate operations
            lines = current_content.splitlines()
            total_lines = len(lines)

            for j, op in enumerate(operations):
                op_type = op.get("type")
                if op_type not in ("replace", "insert", "delete"):
                    return {
                        "applied": [],
                        "failed": [
                            {
                                "index": i,
                                "path": path,
                                "error": f"Operation {j}: invalid type '{op_type}'",
                            }
                        ],
                    }

                if op_type in ("replace", "delete"):
                    start = op.get("start")
                    end = op.get("end")
                    if start is None or end is None:
                        return {
                            "applied": [],
                            "failed": [
                                {
                                    "index": i,
                                    "path": path,
                                    "error": f"Operation {j}: 'start' and 'end' required",
                                }
                            ],
                        }
                    if start < 1:
                        return {
                            "applied": [],
                            "failed": [
                                {
                                    "index": i,
                                    "path": path,
                                    "error": f"Operation {j}: start={start} < 1",
                                }
                            ],
                        }
                    if end > total_lines:
                        return {
                            "applied": [],
                            "failed": [
                                {
                                    "index": i,
                                    "path": path,
                                    "error": f"Operation {j}: end={end} exceeds file length ({total_lines})",
                                }
                            ],
                        }
                    if start > end:
                        return {
                            "applied": [],
                            "failed": [
                                {
                                    "index": i,
                                    "path": path,
                                    "error": f"Operation {j}: start={start} > end={end}",
                                }
                            ],
                        }

                elif op_type == "insert":
                    after = op.get("after")
                    if after is None:
                        return {
                            "applied": [],
                            "failed": [
                                {
                                    "index": i,
                                    "path": path,
                                    "error": f"Operation {j}: 'after' required",
                                }
                            ],
                        }
                    if after < 0 or after > total_lines:
                        return {
                            "applied": [],
                            "failed": [
                                {
                                    "index": i,
                                    "path": path,
                                    "error": f"Operation {j}: after={after} out of range",
                                }
                            ],
                        }

            validated.append((path, current_content, operations))

        # Second pass: apply all edits
        applied = []
        for path, current_content, operations in validated:
            lines = current_content.splitlines()
            line_ops = []

            for op in operations:
                op_type = op["type"]
                if op_type in ("replace", "delete"):
                    line_ops.append((op["start"], op["end"], op_type, op))
                else:  # insert
                    line_ops.append((op["after"], op["after"], op_type, op))

            # Apply in reverse line order
            new_lines = list(lines)
            for _, _, op_type, op in sorted(line_ops, key=lambda x: x[0], reverse=True):
                if op_type == "replace":
                    start = op["start"] - 1
                    end = op["end"]
                    new_content = op.get("lines", [])
                    new_lines[start:end] = new_content
                elif op_type == "delete":
                    start = op["start"] - 1
                    end = op["end"]
                    del new_lines[start:end]
                elif op_type == "insert":
                    after = op["after"]
                    new_content = op.get("lines", [])
                    new_lines[after:after] = new_content

            new_content = "\n".join(new_lines)
            if new_lines:
                new_content += "\n"

            self._shadow.write(path, new_content)
            new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()

            applied.append(
                {
                    "path": path,
                    "new_hash": new_hash,
                    "operations_count": len(operations),
                }
            )

        return {"applied": applied, "failed": []}

    # -------------------------------------------------------------------------
    # Tool: list_files (shadow-aware)
    # -------------------------------------------------------------------------

    def list_files(self, glob: str = "*") -> list[str]:
        """
        List files matching glob pattern within content_dir.

        If shadow is attached, returns merged shadow+live view.

        Args:
            glob: Glob pattern (default: "**/*" lists all files)

        Returns:
            List of relative paths matching the pattern

        Raises:
            ToolError: If glob pattern is invalid
        """
        self._usage["list_files"] += 1

        # Shadow mode: use shadowfs.list
        if self._shadow:
            try:
                return self._shadow.list(glob)
            except Exception as e:
                raise ToolError("list_files", f"List error: {e}") from e

        # Live mode: direct filesystem access
        glob = glob.strip()
        if not glob:
            glob = "**/*"

        try:
            all_files = []
            for file_path in self._content_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                rel_path = file_path.relative_to(self._content_dir).as_posix()
                all_files.append(rel_path)

            if glob == "**/*":
                matches = all_files
            elif "**/" in glob:
                suffix = glob.replace("**/", "")
                matches = [
                    f for f in all_files if fnmatch.fnmatch(f, suffix) or fnmatch.fnmatch(f, glob)
                ]
            elif "*" in glob or "?" in glob:
                matches = [f for f in all_files if fnmatch.fnmatch(f, glob)]
            else:
                matches = [f for f in all_files if f.startswith(glob) or fnmatch.fnmatch(f, glob)]

            return sorted(matches)[:100]

        except Exception as e:
            raise ToolError("list_files", f"List error: {e}") from e

    # -------------------------------------------------------------------------
    # Tool: run_lint
    # -------------------------------------------------------------------------

    def run_lint(
        self,
        paths: list[str] | None = None,
        language: str | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """
        Run semgrep linter on shadow state.

        Optimized to stage only files being linted, with output limits to
        prevent memory/context overflow from large result sets.

        Args:
            paths: Specific files to lint, or None for all shadow writes
            language: Optional language override (auto-detected from extensions)
            intent: Reason for linting (for chronicle)

        Returns:
            {
                "passed": bool,
                "issues": [{
                    "file": str,
                    "line": int,
                    "column": int,
                    "message": str,
                    "severity": "error"|"warning"|"info",
                    "rule_id": str,
                }],
                "attempt_number": int,
                "truncated": bool,  # True if results were capped
            }
        """
        self._usage["run_lint"] = self._usage.get("run_lint", 0) + 1

        # Constants for safety limits (aim: at most a page of output)
        MAX_OUTPUT_BYTES = 20 * 1024  # 20KB stdout/stderr cap
        MAX_ISSUES = 50  # Cap issues to prevent context overflow

        if not self._shadow:
            raise ToolError("run_lint", "ShadowFS not attached")

        # Determine target paths
        if paths is None:
            writes = self._shadow.get_writes()
            if not writes:
                return {
                    "passed": True,
                    "issues": [],
                    "attempt_number": self._usage["run_lint"],
                    "truncated": False,
                }
            target_paths = list(writes.keys())
        else:
            target_paths = paths

        # Filter to existing paths and stage only needed files
        temp_dir = None
        try:
            # Stage only the specific files being linted (not entire shadow)
            temp_dir = self._stage_files_for_lint(target_paths)

            # Build semgrep command
            cmd = [
                "semgrep",
                "--json",
                "--quiet",
                "--config=auto",
            ]
            if language:
                cmd.extend(["--lang", language])

            # Add target paths (relative to temp_dir)
            for p in target_paths:
                target = temp_dir / p
                if target.exists():
                    cmd.append(str(target))

            # Run semgrep with output limits
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Guard against massive output
            if len(result.stdout) > MAX_OUTPUT_BYTES:
                return {
                    "passed": False,
                    "issues": [
                        {
                            "file": "",
                            "line": 0,
                            "column": 0,
                            "message": f"Lint output exceeded {MAX_OUTPUT_BYTES} bytes - too many findings",
                            "severity": "error",
                            "rule_id": "lint-output-overflow",
                        }
                    ],
                    "attempt_number": self._usage["run_lint"],
                    "truncated": True,
                }

            # Parse JSON output with size guard
            stdout_trimmed = result.stdout[:MAX_OUTPUT_BYTES] if result.stdout else ""
            try:
                output = (
                    json.loads(stdout_trimmed) if stdout_trimmed else {"results": [], "errors": []}
                )
            except json.JSONDecodeError:
                output = {"results": [], "errors": []}

            # Transform to structured issues with hard cap
            issues = []
            truncated = False
            temp_dir_str = str(temp_dir)

            for finding in output.get("results", []):
                if len(issues) >= MAX_ISSUES:
                    truncated = True
                    break

                severity = finding.get("extra", {}).get("severity", "info").lower()
                if severity not in ("error", "warning", "info"):
                    severity = "info"

                # Map path back to relative using Path for safety
                full_path = finding.get("path", "")
                rel_path = full_path
                if full_path.startswith(temp_dir_str):
                    rel_path = full_path[len(temp_dir_str) + 1 :]

                # Truncate overly long messages
                message = finding.get("extra", {}).get("message", "")
                if len(message) > 500:
                    message = message[:497] + "..."

                issues.append(
                    {
                        "file": rel_path,
                        "line": finding.get("start", {}).get("line", 0),
                        "column": finding.get("start", {}).get("col", 0),
                        "message": message,
                        "severity": severity,
                        "rule_id": finding.get("check_id", "unknown"),
                    }
                )

            # Add execution errors (capped by remaining budget)
            errors = output.get("errors", [])
            for e in errors:
                if len(issues) >= MAX_ISSUES:
                    truncated = True
                    break

                msg = e.get("message", str(e))
                if len(msg) > 500:
                    msg = msg[:497] + "..."

                issues.append(
                    {
                        "file": "",
                        "line": 0,
                        "column": 0,
                        "message": f"semgrep error: {msg}",
                        "severity": "error",
                        "rule_id": "semgrep-error",
                    }
                )

            # Add stderr if present (with truncation)
            stderr_trimmed = result.stderr[:2000] if result.stderr else ""
            if stderr_trimmed and len(issues) < MAX_ISSUES:
                issues.append(
                    {
                        "file": "",
                        "line": 0,
                        "column": 0,
                        "message": f"semgrep stderr: {stderr_trimmed[:500]}",
                        "severity": "info",
                        "rule_id": "lint-stderr",
                    }
                )

            # passed = True if no error-severity issues
            has_errors = any(i["severity"] == "error" for i in issues)

            return {
                "passed": not has_errors,
                "issues": issues,
                "attempt_number": self._usage["run_lint"],
                "truncated": truncated,
            }

        except subprocess.TimeoutExpired:
            raise ToolError("run_lint", "Lint execution timed out after 60 seconds")
        except FileNotFoundError:
            # semgrep not installed
            return {
                "passed": True,
                "issues": [
                    {
                        "file": "",
                        "line": 0,
                        "column": 0,
                        "message": "semgrep not installed - skipping lint",
                        "severity": "info",
                        "rule_id": "lint-unavailable",
                    }
                ],
                "attempt_number": self._usage["run_lint"],
                "truncated": False,
            }
        except Exception as e:
            raise ToolError("run_lint", f"Lint execution failed: {e}") from e
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _stage_files_for_lint(self, paths: list[str]) -> Path:
        """
        Stage only specific files for linting (not entire shadow).

        Creates minimal temp directory with just the files being linted,
        plus their shadow state if modified.

        Args:
            paths: Relative paths to stage

        Returns:
            Path to temporary directory
        """
        temp_dir = Path(tempfile.mkdtemp(prefix="a7rt_lint_"))

        try:
            for rel_path in paths:
                if not self._shadow:
                    continue

                # Get content from shadow if written, else from live
                try:
                    content = self._shadow.read(rel_path)
                except FileNotFoundError:
                    continue

                # Write to temp directory
                dest = temp_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")

            return temp_dir

        except Exception:
            # Clean up on failure
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def validate_exports(
        self,
        expected: list[str] | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """
        Verify declared exports exist in Python modules.

        Uses runtime import probe to check that interface.exports matches
        actual module exports (__all__ or dir()). Catches typos and missing
        __all__ entries before expensive test cycles.

        Args:
            expected: Export names to validate, or None to use node interface
            intent: Reason for validation (for chronicle)

        Returns:
            {
                "status": "exports_ok" | "exports_missing" | "no_python_files",
                "failures": [{"file": "x.py", "missing": ["Foo", "bar"]}],
                "suggestion": "Add to __all__ or fix interface.exports",
                "attempt_number": int,
            }
        """
        self._usage["validate_exports"] = self._usage.get("validate_exports", 0) + 1

        if not self._shadow:
            raise ToolError("validate_exports", "ShadowFS not attached")

        # Get expected exports - either provided or from node interface
        export_names = expected
        if export_names is None:
            # Would need to read from node interface - for now require explicit
            raise ToolError("validate_exports", "Expected exports must be provided explicitly")

        if not export_names:
            return {
                "status": "exports_ok",
                "failures": [],
                "suggestion": "No exports declared to validate",
                "attempt_number": self._usage["validate_exports"],
            }

        temp_dir = None
        try:
            # Stage shadow to temp for import probe
            temp_dir = self._shadow.stage_to_temp()

            # Get shadow writes to know which files to validate
            shadow_writes = self._shadow.get_writes()

            # Use standalone validator
            result = validate_shadow_exports(
                shadow_writes=shadow_writes,
                interface_exports=export_names,
                temp_dir=temp_dir,
            )

            result["attempt_number"] = self._usage["validate_exports"]
            return result

        except Exception as e:
            raise ToolError("validate_exports", f"Export validation failed: {e}") from e
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Telemetry
    # -------------------------------------------------------------------------

    def get_usage(self) -> dict[str, int]:
        """Return tool call counts for telemetry."""
        return dict(self._usage)

    def get_thoughts(self) -> list[dict]:
        """Return recorded thoughts for persistence across redispatches."""
        return list(self._thoughts)

    def get_current_state(self) -> dict[str, Any]:
        """
        Get summary of current shadow state (Phase 3).

        Returns:
            Dict with files_written, test_status, thoughts_recorded
        """
        self._usage["get_current_state"] += 1

        if not self._shadow:
            return {
                "files_written": [],
                "test_status": None,
                "thoughts_recorded": [],
            }

        writes = self._shadow.get_writes()

        return {
            "files_written": list(writes.keys()),
            "test_status": self._last_test_result,
            "thoughts_recorded": self._thoughts,
        }

    def reset_usage(self) -> None:
        """Reset usage counters and thoughts (called at start of each dispatch)."""
        self._usage = {
            "read_file": 0,
            "write_file": 0,
            "delete_file": 0,
            "rename_file": 0,
            "edit_file": 0,
            "edit_files": 0,
            "run_lint": 0,
            "grep_content": 0,
            "list_files": 0,
            "preview_file": 0,
            "record_thought": 0,
            "run_test": 0,
            "validate_exports": 0,
            "get_current_state": 0,
        }
        self._thoughts = []
        self._last_test_result = None

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _resolve_path(self, path: str) -> Path:
        """
        Resolve a relative path within content_dir.

        Raises ToolError if path attempts to escape content_dir.
        """
        # Reject absolute paths
        if path.startswith("/"):
            raise ToolError("read_file", f"Absolute paths not allowed: {path}")

        # Normalize path
        target = (self._content_dir / path).resolve()

        # Security check: ensure resolved path is within content_dir
        try:
            target.relative_to(self._content_dir)
        except ValueError:
            raise ToolError("read_file", f"Path escapes content_dir: {path}") from None

        return target

    def _is_binary(self, path: Path) -> bool:
        """Check if file is likely binary by extension."""
        binary_extensions = {
            ".pyc",
            ".pyo",
            ".so",
            ".dll",
            ".exe",
            ".bin",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".zip",
            ".tar",
            ".gz",
        }
        return path.suffix.lower() in binary_extensions


# -------------------------------------------------------------------------
# Inline tests
# -------------------------------------------------------------------------


def _test_agent_tools() -> None:
    """Run inline tests for AgentTools."""
    import tempfile

    print("Running agent_tools tests...")

    with tempfile.TemporaryDirectory() as tmpdir:
        content_dir = Path(tmpdir) / "content"
        content_dir.mkdir()

        # Create test files
        (content_dir / "auth").mkdir()
        (content_dir / "auth" / "handler.py").write_text(
            "def authenticate(token):\n    return verify(token)\n"
        )
        (content_dir / "auth" / "jwt.py").write_text("def verify(token):\n    return payload\n")
        (content_dir / "utils.py").write_text("def helper():\n    pass\n")

        tools = AgentTools(content_dir)

        # Test read_file
        content = tools.read_file("auth/handler.py")
        assert "def authenticate" in content
        print("  [OK] read_file: basic read")

        # Test read_file error on escape
        try:
            tools.read_file("../outside.txt")
            assert False, "Should have raised ToolError"
        except ToolError as e:
            assert "escapes content_dir" in str(e)
        print("  [OK] read_file: path escape blocked")

        # Test read_file error on absolute path
        try:
            tools.read_file("/etc/passwd")
            assert False, "Should have raised ToolError"
        except ToolError as e:
            assert "Absolute paths not allowed" in str(e)
        print("  [OK] read_file: absolute path blocked")

        # Test grep_content
        tools.reset_usage()
        matches = tools.grep_content(r"def \w+")
        assert len(matches) >= 3  # authenticate, verify, helper
        assert any(m["path"] == "auth/handler.py" for m in matches)
        print("  [OK] grep_content: pattern matching")

        # Test grep_content with line numbers
        auth_matches = [m for m in matches if m["path"] == "auth/handler.py"]
        assert any(m["line_number"] == 1 for m in auth_matches)
        print("  [OK] grep_content: line numbers correct")

        # Test list_files
        tools.reset_usage()
        files = tools.list_files("**/*.py")
        assert "auth/handler.py" in files
        assert "auth/jwt.py" in files
        assert "utils.py" in files
        print("  [OK] list_files: glob pattern")

        # Test list_files subdirectory
        tools.reset_usage()
        auth_files = tools.list_files("auth/*")
        assert "auth/handler.py" in auth_files
        assert "utils.py" not in auth_files
        print("  [OK] list_files: subdirectory")

        # Test telemetry
        tools.reset_usage()
        tools.read_file("utils.py")
        tools.grep_content("def")
        tools.list_files("*.py")
        usage = tools.get_usage()
        assert usage["read_file"] == 1
        assert usage["grep_content"] == 1
        assert usage["list_files"] == 1
        print("  [OK] telemetry: usage tracking")

        # Test preview_file
        tools.reset_usage()
        result = tools.preview_file("auth/handler.py", offset=0, max_lines=10)
        assert result["path"] == "auth/handler.py"
        assert result["total_lines"] == 2
        assert len(result["lines"]) == 2
        assert result["lines"][0]["line_number"] == 1
        assert "def authenticate" in result["lines"][0]["content"]
        print("  [OK] preview_file: basic read")

        # Test preview_file with offset
        tools.reset_usage()
        result = tools.preview_file("auth/handler.py", offset=1, max_lines=10)
        assert result["offset"] == 1
        assert len(result["lines"]) == 1
        assert result["lines"][0]["line_number"] == 2
        print("  [OK] preview_file: offset")

        # Test preview_file out of bounds
        tools.reset_usage()
        result = tools.preview_file("auth/handler.py", offset=100, max_lines=10)
        assert result["lines"] == []
        assert result["total_lines"] == 2
        print("  [OK] preview_file: out of bounds")

        # Test preview_file max_lines clamping
        tools.reset_usage()
        result = tools.preview_file("auth/handler.py", offset=0, max_lines=1)
        assert len(result["lines"]) == 1
        print("  [OK] preview_file: max_lines clamping")

        # Test preview_file error on escape
        try:
            tools.preview_file("../outside.txt")
            assert False, "Should have raised ToolError"
        except ToolError as e:
            assert "escapes content_dir" in str(e)
        print("  [OK] preview_file: path escape blocked")

    print("\nAll agent_tools tests passed.")


if __name__ == "__main__":
    _test_agent_tools()
