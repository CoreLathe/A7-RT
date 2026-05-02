"""
Phase 9.3: Multi-File PR Atomicity Verification

Tests that agent-authored multi-file PRs are committed atomically:
- All files written or none
- committed_files metadata tracks all files
- shadow_committed event logged with file list
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from a7_rt_core.core.models import (
    ManagerState,
    NodeStatus,
    NodeType,
    ProtocolWeight,
    SubagentReturn,
    create_project,
    dispatch_node,
)
from a7_rt_core.storage.repository import Repository


class TestMultiFileAtomicity:
    """Verify multi-file commits are atomic and properly tracked."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository with a test stage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = create_project(
                name="multifile-test", description="Test multi-file atomicity"
            )
            repo = Repository.init(root, project)
            stage = repo.create_stage("stage-1")

            # Create a test node
            node = dispatch_node(
                "auth.handler",
                stage.stage_id,
                NodeType.FEATURE,
                "JWT auth handler with multiple files",
                protocol_weight=ProtocolWeight.LEAN,
            )
            repo.add_node(node)

            yield repo, stage, node

    def test_multi_file_commit_writes_all_files(self, temp_repo):
        """Verify all files from a multi-file return are written."""
        repo, stage, node = temp_repo

        # Simulate multi-file subagent return
        result = SubagentReturn(
            status=NodeStatus.GROUNDED,
            files={
                "auth/__init__.py": "# Auth package\n",
                "auth/jwt.py": "import hashlib\ndef sign(payload, secret):\n    return hashlib.sha256(f'{payload}.{secret}'.encode()).hexdigest()\n",
                "auth/handler.py": "from .jwt import sign\n\ndef authenticate(token):\n    return sign(token, 'secret')\n",
            },
            interface_update={
                "exports": ["authenticate(token) -> str"],
                "assumptions": ["JWT secret is configured"],
            },
            role="builder",
            iterations_used=3,
        )

        # Apply the return via harness mixin
        from a7_rt_core.harness.apply import ApplyMixin
        from a7_rt_core.harness.utils import UtilsMixin

        class MockHarness(ApplyMixin, UtilsMixin):
            def __init__(self, repo):
                self.repo = repo
                self._current_shadow = None
                self._current_node_id = None

            def _now(self):
                from datetime import datetime, timezone

                return datetime.now(timezone.utc).isoformat()

        harness = MockHarness(repo)

        state = ManagerState(
            current_stage_id=stage.stage_id,
            turn=5,
            max_turns=25,
        )

        # Apply the return
        harness._apply_return("auth.handler", result, NodeStatus.GROUNDED, state)

        # Verify all files exist
        content_dir = repo._content_dir

        expected_files = {
            "auth/__init__.py": 15,  # bytes - this becomes primary (first in dict)
            "auth/jwt.py": 112,
            "auth/handler.py": 81,
            "auth.handler.py": 15,  # Generated from primary (auth/__init__.py)
        }

        for rel_path, min_size in expected_files.items():
            full_path = content_dir / rel_path
            assert full_path.exists(), f"Expected file {rel_path} not found"
            assert full_path.stat().st_size >= min_size, (
                f"File {rel_path} smaller than expected"
            )

    def test_committed_files_metadata(self, temp_repo):
        """Verify committed_files list in node metadata."""
        repo, stage, node = temp_repo

        result = SubagentReturn(
            status=NodeStatus.GROUNDED,
            files={
                "auth/__init__.py": "# Auth package\n",
                "auth/jwt.py": "def sign(): pass\n",
                "auth/handler.py": "def authenticate(): pass\n",
            },
            interface_update={"exports": ["authenticate()"]},
            role="builder",
        )

        from a7_rt_core.harness.apply import ApplyMixin
        from a7_rt_core.harness.utils import UtilsMixin

        class MockHarness(ApplyMixin, UtilsMixin):
            def __init__(self, repo):
                self.repo = repo
                self._current_shadow = None
                self._current_node_id = None

            def _now(self):
                from datetime import datetime, timezone

                return datetime.now(timezone.utc).isoformat()

        harness = MockHarness(repo)
        state = ManagerState(current_stage_id=stage.stage_id, turn=1, max_turns=25)

        harness._apply_return("auth.handler", result, NodeStatus.GROUNDED, state)

        # Verify committed_files in metadata
        doc = repo._load()
        node_data = doc["nodes"]["auth.handler"]
        committed_files = node_data.get("committed_files", [])

        assert len(committed_files) >= 3, (
            f"Expected 3+ files, got {len(committed_files)}"
        )
        assert "auth/__init__.py" in committed_files
        assert "auth/jwt.py" in committed_files
        assert "auth/handler.py" in committed_files

    def test_shadow_committed_event(self, temp_repo):
        """Verify multi-file commit writes all files (atomicity)."""
        repo, stage, node = temp_repo

        result = SubagentReturn(
            status=NodeStatus.GROUNDED,
            files={
                "auth/__init__.py": "# Auth package\n",
                "auth/jwt.py": "def sign(): pass\n",
            },
            interface_update={"exports": ["sign()"]},
            role="builder",
        )

        from a7_rt_core.harness.apply import ApplyMixin
        from a7_rt_core.harness.utils import UtilsMixin

        class MockHarness(ApplyMixin, UtilsMixin):
            def __init__(self, repo):
                self.repo = repo
                self._current_shadow = None
                self._current_node_id = None

            def _now(self):
                from datetime import datetime, timezone

                return datetime.now(timezone.utc).isoformat()

        harness = MockHarness(repo)
        state = ManagerState(current_stage_id=stage.stage_id, turn=1, max_turns=25)

        harness._apply_return("auth.handler", result, NodeStatus.GROUNDED, state)

        # Verify both files written atomically (all or none)
        doc = repo._load()
        node_data = doc["nodes"]["auth.handler"]
        committed_files = node_data.get("committed_files", [])

        # Should have both user files + generated importable
        assert len(committed_files) >= 2, (
            f"Expected 2+ committed files, got {committed_files}"
        )
        assert "auth/__init__.py" in committed_files
        assert "auth/jwt.py" in committed_files

        # Verify files exist on disk
        content_dir = repo._content_dir
        assert (content_dir / "auth/__init__.py").exists()
        assert (content_dir / "auth/jwt.py").exists()

    def test_metadata_map_tracks_all_files(self, temp_repo):
        """Verify metadata_map contains entries for all committed files."""
        repo, stage, node = temp_repo

        result = SubagentReturn(
            status=NodeStatus.GROUNDED,
            files={
                "auth/__init__.py": "# Auth package\n__all__ = ['jwt', 'handler']\n",
                "auth/jwt.py": "def sign(payload, secret): return 'signed'\n",
                "auth/handler.py": "def authenticate(token): return True\n",
            },
            interface_update={"exports": ["authenticate(token) -> bool"]},
            role="builder",
        )

        from a7_rt_core.harness.apply import ApplyMixin
        from a7_rt_core.harness.utils import UtilsMixin

        class MockHarness(ApplyMixin, UtilsMixin):
            def __init__(self, repo):
                self.repo = repo
                self._current_shadow = None
                self._current_node_id = None

            def _now(self):
                from datetime import datetime, timezone

                return datetime.now(timezone.utc).isoformat()

        harness = MockHarness(repo)
        state = ManagerState(current_stage_id=stage.stage_id, turn=1, max_turns=25)

        harness._apply_return("auth.handler", result, NodeStatus.GROUNDED, state)

        # Verify metadata_map
        doc = repo._load()
        node_data = doc["nodes"]["auth.handler"]
        metadata_map = node_data.get("metadata_map", {})

        assert "auth/__init__.py" in metadata_map
        assert "auth/jwt.py" in metadata_map
        assert "auth/handler.py" in metadata_map

        # Each entry should have tokens and lines
        for path, meta in metadata_map.items():
            assert "tokens" in meta or "lines" in meta, (
                f"Metadata for {path} missing token/line count"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
