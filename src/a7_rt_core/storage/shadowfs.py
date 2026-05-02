"""
A7-RT ShadowFS — Transactional Filesystem Layer for Agent Dispatch

Write-accumulator filesystem shadow. All agent modifications are staged
until COMMIT. Reads see shadow state (including agent's own writes).
Tests execute against shadow staging. submit_pr commits shadow to live disk.

Invariants:
- No live modification until COMMIT
- Agent reads see own writes (shadow priority)
- Rename chains resolve correctly
- Deleted files are tombstoned
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any


class ShadowFSError(Exception):
    """ShadowFS-specific errors (path escapes, invalid operations, etc.)."""

    pass


class ShadowFS:
    """
    Write-accumulator filesystem layer for agent dispatch.

    Tracks agent modifications without copying unmodified files.
    Live files are read on-demand. All modifications staged until COMMIT.
    """

    def __init__(self, base_dir: Path | str):
        """
        Initialize ShadowFS with base directory.

        Args:
            base_dir: Root directory for live file operations
        """
        self.base = Path(base_dir).resolve()
        self._writes: dict[str, str] = {}  # path -> content (agent-written only)
        self._deleted: set[str] = set()  # tombstones
        self._renamed: dict[str, str] = {}  # old_path -> new_path

    def read(self, path: str) -> str:
        """
        Read file content. Checks writes first, then live.

        Args:
            path: Relative path from base_dir

        Returns:
            File content as string

        Raises:
            FileNotFoundError: If file doesn't exist or was deleted
            ShadowFSError: If path escapes base_dir
        """
        # Resolve rename chain (forward resolution for source paths)
        resolved_path = self._resolve_rename_chain(path)

        if resolved_path in self._writes:
            return self._writes[resolved_path]

        if resolved_path in self._deleted:
            raise FileNotFoundError(f"{path} was deleted in this session")

        # Check if this path is a target of a rename (backward lookup)
        # If so, we need to read from the source (live file)
        source_path = self._get_source_for_target(resolved_path)
        read_path = source_path if source_path else resolved_path

        # Read from live filesystem
        live_path = self._resolve_live_path(read_path)
        if not live_path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        return live_path.read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        """
        Write to shadow layer only.

        Args:
            path: Relative path from base_dir
            content: File content to write

        Raises:
            ShadowFSError: If path escapes base_dir
        """
        # Security check first
        self._resolve_live_path(path)

        # Resolve rename chain to find current shadow name
        resolved_path = self._resolve_rename_chain(path)

        self._writes[resolved_path] = content
        self._deleted.discard(resolved_path)

    def delete(self, path: str) -> None:
        """
        Mark file as deleted in shadow.

        Args:
            path: Relative path from base_dir

        Raises:
            ShadowFSError: If path escapes base_dir
        """
        # Security check first
        self._resolve_live_path(path)

        # Resolve rename chain
        resolved_path = self._resolve_rename_chain(path)

        self._deleted.add(resolved_path)
        self._writes.pop(resolved_path, None)

    def rename(self, old_path: str, new_path: str) -> None:
        """
        Rename file in shadow.

        Handles rename chains: if old_path was already renamed,
        the chain is updated to point to new_path.

        Args:
            old_path: Original relative path
            new_path: New relative path

        Raises:
            ShadowFSError: If paths escape base_dir
        """
        # Security check first
        self._resolve_live_path(old_path)
        self._resolve_live_path(new_path)

        # Resolve the old_path through existing rename chain
        resolved_old = self._resolve_rename_chain(old_path)

        # Update rename mapping: point original old_path to new_path
        # But we need to update any chains that pointed to resolved_old
        self._renamed[resolved_old] = new_path

        # If resolved_old was in writes, move to new_path
        if resolved_old in self._writes:
            self._writes[new_path] = self._writes.pop(resolved_old)

        # If resolved_old was deleted, mark new_path as deleted
        if resolved_old in self._deleted:
            self._deleted.add(new_path)
            self._deleted.discard(resolved_old)

        # Update any existing rename chains that point to resolved_old
        # They should now point to new_path
        for orig, target in list(self._renamed.items()):
            if target == resolved_old and orig != resolved_old:
                self._renamed[orig] = new_path

    def list(self, pattern: str = "*") -> list[str]:
        """
        List files matching pattern. Merges live + shadow operations.

        Args:
            pattern: Glob pattern like "**/*.py" or "auth/*.py"

        Returns:
            Sorted list of relative paths
        """
        # Get live files - use **/* to find all, then filter by pattern
        live_matches: set[str] = set()
        if self.base.exists():
            search_pattern = "**/*" if pattern == "*" else pattern
            for p in self.base.glob(search_pattern):
                if p.is_file():
                    rel_path = str(p.relative_to(self.base))
                    if Path(rel_path).match(pattern):
                        live_matches.add(rel_path)

        # Start with live files minus deleted
        result = live_matches - self._deleted

        # Add shadow writes
        result = result | set(self._writes.keys())

        # Apply renames: remove old names, add new names if they match pattern
        for old_path, new_path in self._renamed.items():
            result.discard(old_path)
            if Path(new_path).match(pattern):
                result.add(new_path)

        return sorted(result)

    def stage_to_temp(self) -> Path:
        """
        Create temp directory with full filesystem state.

        Creates a complete, isolated environment for test execution
        or commit preview. Includes:
        - Live files (excluding deleted, applying renames)
        - Shadow-written files
        - Applied renames

        Returns:
            Path to temporary directory

        Raises:
            ShadowFSError: If staging fails
        """
        temp_dir = Path(tempfile.mkdtemp(prefix="a7rt_shadow_"))

        try:
            # Track which live files have been handled
            handled_live: set[str] = set()

            # First, handle renamed live files: copy from source to target
            for old_path, new_path in self._renamed.items():
                if old_path in self._deleted:
                    continue  # Don't copy deleted files
                live_file = self.base / old_path
                if live_file.exists():
                    dest = temp_dir / new_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live_file, dest)
                    handled_live.add(old_path)

            # Copy remaining live files (not renamed, not deleted, not shadow-written)
            if self.base.exists():
                for live_file in self.base.rglob("*"):
                    if live_file.is_file():
                        rel_path = str(live_file.relative_to(self.base))
                        if rel_path in handled_live:
                            continue
                        if rel_path in self._deleted:
                            continue
                        if rel_path in self._writes:
                            continue  # Shadow write will override
                        dest = temp_dir / rel_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(live_file, dest)

            # Write shadow files (these override any live copies)
            for rel_path, content in self._writes.items():
                dest = temp_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")

            return temp_dir

        except Exception as e:
            # Clean up on failure
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ShadowFSError(f"Failed to stage shadow: {e}") from e

    def get_writes(self) -> dict[str, str]:
        """
        Return written file contents for commit.

        Returns:
            Dict mapping relative paths to content strings
        """
        return dict(self._writes)

    def get_deletions(self) -> set[str]:
        """
        Return set of deleted paths.

        Returns:
            Set of relative paths marked for deletion
        """
        return set(self._deleted)

    def get_renames(self) -> dict[str, str]:
        """
        Return rename mappings.

        Returns:
            Dict mapping old paths to new paths
        """
        return dict(self._renamed)

    def cleanup(self) -> None:
        """Clear all shadow state."""
        self._writes.clear()
        self._deleted.clear()
        self._renamed.clear()

    def has_changes(self) -> bool:
        """
        Check if shadow has any modifications.

        Returns:
            True if there are writes, deletes, or renames
        """
        return bool(self._writes or self._deleted or self._renamed)

    def get_status(self, path: str) -> dict[str, Any]:
        """
        Get the shadow status of a path.

        Args:
            path: Relative path from base_dir

        Returns:
            Dict with status info:
            - exists: bool (in shadow or live)
            - in_shadow: bool (modified in shadow)
            - is_deleted: bool
            - is_renamed: bool (has been renamed)
            - shadow_path: str (current shadow path after renames)
        """
        resolved = self._resolve_rename_chain(path)

        status = {
            "exists": False,
            "in_shadow": False,
            "is_deleted": False,
            "is_renamed": False,
            "shadow_path": resolved,
        }

        # Check if renamed
        if path in self._renamed or resolved != path:
            status["is_renamed"] = True

        # Check shadow state
        if resolved in self._writes:
            status["in_shadow"] = True
            status["exists"] = True

        if resolved in self._deleted:
            status["is_deleted"] = True
            status["exists"] = False

        # Check live if not deleted and not in writes
        if not status["exists"] and not status["is_deleted"]:
            live_path = self._resolve_live_path(resolved)
            status["exists"] = live_path.exists()

        return status

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _resolve_rename_chain(self, path: str) -> str:
        """
        Resolve a path through rename chains (forward resolution).

        If a->b and b->c, then _resolve_rename_chain("a") returns "c".

        Args:
            path: Starting path

        Returns:
            Final path after following rename chain
        """
        seen = set()
        current = path

        while current in self._renamed:
            if current in seen:
                # Cycle detected - break it
                break
            seen.add(current)
            current = self._renamed[current]

        return current

    def _get_source_for_target(self, target: str) -> str | None:
        """
        Find which path maps to a given target (backward lookup).

        If a->b, then _get_source_for_target("b") returns "a".

        Args:
            target: The target path to look up

        Returns:
            Source path if found, None otherwise
        """
        for source, dest in self._renamed.items():
            if dest == target:
                return source
        return None

    def _get_target_for_source(self, source: str) -> str | None:
        """
        Get the immediate target for a source path.

        If a->b, then _get_target_for_source("a") returns "b".

        Args:
            source: The source path

        Returns:
            Target path if renamed, None otherwise
        """
        return self._renamed.get(source)

    def _resolve_live_path(self, rel_path: str) -> Path:
        """
        Resolve relative path to absolute path within base_dir.

        Args:
            rel_path: Relative path

        Returns:
            Absolute Path

        Raises:
            ShadowFSError: If path escapes base_dir
        """
        # Normalize and check for path traversal
        target = (self.base / rel_path).resolve()

        # Security check: ensure path is within base_dir
        try:
            target.relative_to(self.base)
        except ValueError:
            raise ShadowFSError(f"Path escapes content directory: {rel_path}")

        return target


# -------------------------------------------------------------------------
# Self-test
# -------------------------------------------------------------------------


def _test_shadowfs():
    """Quick sanity test for ShadowFS."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shadow = ShadowFS(base)

        # Create a live file
        (base / "live.txt").write_text("live content")

        # Test read from live
        assert shadow.read("live.txt") == "live content"

        # Test write to shadow
        shadow.write("shadow.txt", "shadow content")
        assert shadow.read("shadow.txt") == "shadow content"
        assert not (base / "shadow.txt").exists()  # Not written to live yet

        # Test shadow overrides live
        shadow.write("live.txt", "overridden")
        assert shadow.read("live.txt") == "overridden"
        assert (base / "live.txt").read_text() == "live content"  # Live unchanged

        # Test delete
        shadow.delete("live.txt")
        try:
            shadow.read("live.txt")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass

        # Test list
        files = shadow.list()
        assert "shadow.txt" in files
        assert "live.txt" not in files  # Deleted

        # Test rename (transparent chain resolution)
        shadow.write("original.txt", "original")
        shadow.rename("original.txt", "renamed.txt")
        assert shadow.read("renamed.txt") == "original"
        # Original path still resolves through rename chain
        assert shadow.read("original.txt") == "original"

        # Test rename chain (a->b->c, reading a returns c's content)
        shadow.rename("renamed.txt", "final.txt")
        assert shadow.read("final.txt") == "original"
        assert shadow.read("original.txt") == "original"  # Chain resolves
        assert shadow.read("renamed.txt") == "original"  # Chain resolves

        # Test stage_to_temp
        shadow2 = ShadowFS(base)
        shadow2.write("staged.txt", "staged content")
        temp = shadow2.stage_to_temp()
        assert (temp / "staged.txt").read_text() == "staged content"
        assert (temp / "live.txt").read_text() == "live content"  # From live
        shutil.rmtree(temp)

        # Test get_writes
        writes = shadow.get_writes()
        assert "shadow.txt" in writes
        assert writes["shadow.txt"] == "shadow content"

        # Test cleanup
        shadow.cleanup()
        assert not shadow.has_changes()

        print("✓ ShadowFS tests passed")


if __name__ == "__main__":
    _test_shadowfs()
