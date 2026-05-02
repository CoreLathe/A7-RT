"""
A7-RT Repository Layer

Atomic file I/O for all project state. Manages the master document, manager
lifecycle, stage operations, node updates, and the append-only event log.

Directory layout managed by this module:
  <root>/
  ├── master.json     — project, stages, nodes, dependencies, graveyard
  ├── manager.json   — ManagerState (present ↔ manager is alive)
  ├── events.jsonl    — append-only decision log
  └── content/        — node work-product files (keyed by node_id)

All mutations to master.json and manager.json are atomic: write to a temp file
in the same directory, then os.replace() — which is atomic on POSIX and Windows
(same-volume move). The events log is append-only; each line is flushed and
fsynced individually so partial writes cannot corrupt prior entries.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a7_rt_core.core.models import (
    AgentSession,
    Dependency,
    InterfaceContract,
    ManagerMode,
    ManagerState,
    Node,
    NodeStatus,
    Project,
    Stage,
    StageStatus,
)
from a7_rt_core.core.models import (
    create_stage as _make_stage,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RepositoryError(Exception):
    """Base for all repository-layer errors."""


class InvariantViolation(RepositoryError):
    """Raised when an A7-RT invariant would be violated by the requested operation."""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class Repository:
    """
    Manages the on-disk state of a single A7-RT project.

    All public write methods are atomic. Internal helpers are prefixed with _.
    """

    _MASTER = "master.json"
    _MANAGER = "manager.json"
    _EVENTS = "events.jsonl"
    _CONTENT = "content"
    _SESSIONS = ".sessions"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._master_path = self.root / self._MASTER
        self._manager_path = self.root / self._MANAGER
        self._events_path = self.root / self._EVENTS
        self._content_dir = self.root / self._CONTENT
        self._sessions_dir = self.root / self._CONTENT / self._SESSIONS

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------

    @classmethod
    def init(cls, root: Path, project: Project) -> "Repository":
        """
        Bootstrap a new project directory from a Project model.
        Raises RepositoryError if a repository already exists at root.
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        repo = cls(root)

        if repo._master_path.exists():
            raise RepositoryError(
                f"Repository already exists at {root}. Use Repository.open() to load it."
            )

        doc: dict[str, Any] = {
            "project": project.model_dump(mode="json"),
            "stages": {},
            "nodes": {},
            "dependencies": [],
            "graveyard": [],
        }
        repo._atomic_write_json(repo._master_path, doc)
        repo._events_path.touch()
        repo._content_dir.mkdir(exist_ok=True)
        repo._sessions_dir.mkdir(exist_ok=True)

        # Create conftest.py for pytest to discover .test files
        conftest_content = '''"""Pytest configuration for A7-RT test discovery."""

# Configure pytest to discover .test files
python_files = ["*.test", "test_*.py", "*_test.py"]
'''
        conftest_path = repo._content_dir / "conftest.py"
        repo._atomic_write_content(conftest_path, conftest_content)

        return repo

    @classmethod
    def open(cls, root: Path) -> "Repository":
        """Open an existing project repository."""
        root = Path(root)
        repo = cls(root)
        if not repo._master_path.exists():
            raise RepositoryError(
                f"No repository at {root} — master.json missing. "
                "Use Repository.init() to create one."
            )
        return repo

    # -------------------------------------------------------------------------
    # Internal I/O helpers
    # -------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        """Load and return master.json as a plain dict."""
        with open(self._master_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, doc: dict[str, Any]) -> None:
        """Atomically overwrite master.json."""
        self._atomic_write_json(self._master_path, doc)

    def _atomic_write_json(self, target: Path, data: dict[str, Any]) -> None:
        """
        Write *data* as JSON to *target* atomically.
        Creates a sibling temp file, then os.replace() — atomic on POSIX.
        Temp file is cleaned up on any failure before the rename.
        """
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _atomic_write_content(self, target: Path, text: str) -> None:
        """
        Write plain text to *target* atomically (same pattern as _atomic_write_json).
        Used for node content files in the content/ directory.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _append_event_line(self, line: str) -> None:
        """
        Append a single JSON line to events.jsonl.
        Each line is individually flushed and fsynced so prior entries survive
        a crash during a write.
        """
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    # -------------------------------------------------------------------------
    # Stage operations
    # -------------------------------------------------------------------------

    def create_stage(self, name: str) -> Stage:
        """
        Create a new active stage and append it to the project.

        Invariant: only one active stage may exist at a time.
        Raises InvariantViolation if another stage is currently active.
        """
        doc = self._load()

        for s in doc["stages"].values():
            if s["status"] == StageStatus.ACTIVE.value:
                raise InvariantViolation(
                    f"Cannot create stage '{name}': stage '{s['name']}' ({s['stage_id'][:8]}…) "
                    "is already active. Seal it before creating a new stage."
                )

        project = Project.model_validate(doc["project"])
        stage = _make_stage(project_id=project.project_id, name=name)

        doc["stages"][stage.stage_id] = stage.model_dump(mode="json")
        doc["project"]["stage_ids"] = doc["project"].get("stage_ids", []) + [stage.stage_id]
        self._save(doc)
        return stage

    def seal_stage(
        self,
        stage_id: str,
        summary: str,
        exported_interfaces: dict[str, InterfaceContract],
    ) -> Stage:
        """
        Seal a completed stage.

        Invariants:
          - Stage must exist and be active.
          - No nodes may remain in 'near' status (stage gate).
          - No unverified assumption dependency edges may remain.
        """
        doc = self._load()

        if stage_id not in doc["stages"]:
            raise RepositoryError(f"Stage '{stage_id}' not found.")

        stage_data = doc["stages"][stage_id]
        if stage_data["status"] == StageStatus.SEALED.value:
            raise RepositoryError(f"Stage '{stage_id}' is already sealed.")

        # Stage gate: no 'near' nodes remaining
        node_ids: list[str] = stage_data.get("node_ids", [])
        near_nodes = [
            nid
            for nid in node_ids
            if doc["nodes"].get(nid, {}).get("status") == NodeStatus.NEAR.value
        ]
        if near_nodes:
            raise InvariantViolation(
                f"Cannot seal stage '{stage_id}': {len(near_nodes)} node(s) still 'near': "
                f"{near_nodes}. All nodes must be resolved (grounded, provisional, "
                "suspended, or poisoned) before sealing."
            )

        # Stage gate: no suspended-NEAR nodes remaining.
        # suspended+near means "retry expected" — the node is not resolved, just
        # temporarily blocked. suspended+far/void/wild are terminal enough to seal past.
        suspended_near_nodes = [
            nid
            for nid in node_ids
            if doc["nodes"].get(nid, {}).get("status") == NodeStatus.SUSPENDED.value
            and (doc["nodes"].get(nid, {}).get("suspension_reason") or {}).get("type") == "near"
        ]
        if suspended_near_nodes:
            raise InvariantViolation(
                f"Cannot seal stage '{stage_id}': {len(suspended_near_nodes)} node(s) "
                f"suspended with type='near' (retry expected): {suspended_near_nodes}. "
                "Resolve or re-suspend as far/void/wild before sealing."
            )

        # Stage gate: no unverified assumption edges
        node_id_set = set(node_ids)
        unverified = [
            d
            for d in doc.get("dependencies", [])
            if d.get("type") == "assumption"
            and not d.get("verified", True)
            and d.get("from_node") in node_id_set
        ]
        if unverified:
            raise InvariantViolation(
                f"Cannot seal stage '{stage_id}': {len(unverified)} unverified assumption "
                "edge(s) must be confirmed or removed by a human before sealing."
            )

        now = datetime.now(timezone.utc).isoformat()
        stage_data["status"] = StageStatus.SEALED.value
        stage_data["sealed_at"] = now
        stage_data["summary"] = summary
        stage_data["exported_interfaces"] = {
            k: v.model_dump() for k, v in exported_interfaces.items()
        }
        self._save(doc)
        return Stage.model_validate(stage_data)

    # -------------------------------------------------------------------------
    # Node operations
    # -------------------------------------------------------------------------

    def add_node(self, node: Node) -> None:
        """
        Persist a new node to master.json and register it in its stage.
        Raises RepositoryError if node_id already exists or stage not found.
        """
        doc = self._load()

        if node.node_id in doc["nodes"]:
            raise RepositoryError(
                f"Node '{node.node_id}' already exists. Use update_node() to modify existing nodes."
            )
        if node.stage_id not in doc["stages"]:
            raise RepositoryError(
                f"Stage '{node.stage_id}' not found. Create it before adding nodes."
            )

        doc["nodes"][node.node_id] = node.model_dump(mode="json")
        stage = doc["stages"][node.stage_id]
        stage.setdefault("node_ids", [])
        if node.node_id not in stage["node_ids"]:
            stage["node_ids"].append(node.node_id)

        self._save(doc)

    def update_node(self, node_id: str, **updates: Any) -> Node:
        """
        Apply field updates to an existing node.

        If 'status' is in *updates*, the change is routed through
        Node.transition() so all status-transition invariants are enforced.
        Additional keyword args are applied alongside the status change.

        Returns the updated Node model.
        Raises ValueError (from Node.transition) for illegal status transitions.
        Raises RepositoryError if the node is not found.
        """
        doc = self._load()

        if node_id not in doc["nodes"]:
            raise RepositoryError(f"Node '{node_id}' not found.")

        current = Node.model_validate(doc["nodes"][node_id])

        if "status" in updates:
            new_status = updates.pop("status")
            # transition() calls model_validate internally, re-running all validators
            updated = current.transition(new_status, **updates)
        else:
            # Non-status field updates: reconstruct via model_validate
            # Special handling: if metadata is a dict, convert to NodeMetadata first
            if "metadata" in updates and isinstance(updates["metadata"], dict):
                from a7_rt_core.core.models import NodeMetadata

                updates["metadata"] = NodeMetadata(**updates["metadata"])
            data = current.model_dump(mode="json")
            data.update(updates)
            updated = Node.model_validate(data)

        doc["nodes"][node_id] = updated.model_dump(mode="json")
        self._save(doc)
        return updated

    # -------------------------------------------------------------------------
    # Dependency operations
    # -------------------------------------------------------------------------

    def add_dependency(self, dep: Dependency) -> None:
        """Append a dependency edge to master.json."""
        doc = self._load()
        doc.setdefault("dependencies", [])
        doc["dependencies"].append(dep.model_dump(mode="json"))
        self._save(doc)

    def verify_assumption(self, from_node: str, to_node: str) -> None:
        """Mark a specific assumption edge as verified by a human."""
        doc = self._load()
        changed = False
        for d in doc.get("dependencies", []):
            if (
                d.get("from_node") == from_node
                and d.get("to_node") == to_node
                and d.get("type") == "assumption"
            ):
                d["verified"] = True
                changed = True
        if not changed:
            raise RepositoryError(f"No assumption edge from '{from_node}' to '{to_node}' found.")
        self._save(doc)

    # -------------------------------------------------------------------------
    # Events (append-only)
    # -------------------------------------------------------------------------

    def append_event(self, event: dict[str, Any]) -> None:
        """
        Append a structured event to events.jsonl.
        The log is never edited — this is the immutable audit trail.
        """
        line = json.dumps(event, default=str)
        self._append_event_line(line)

    def get_events(
        self,
        since_turn: int | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Read events.jsonl and return matching events as a list of dicts.

        since_turn: if given, return only events where event["turn"] >= since_turn.
        event_type: if given, return only events where event["action"] == event_type.
        Malformed lines are silently skipped (append-only log; partial write is possible).
        Returns [] if events.jsonl does not exist.
        """
        if not self._events_path.exists():
            return []
        results: list[dict[str, Any]] = []
        for line in self._events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_turn is not None and event.get("turn", 0) < since_turn:
                continue
            if event_type is not None and event.get("action") != event_type:
                continue
            results.append(event)
        return results

    # -------------------------------------------------------------------------
    # Views
    # -------------------------------------------------------------------------

    def get_board_view(self) -> dict[str, Any]:
        """
        Return the manager's view of the active stage.

        Delegates to context.manager_view() so the shape is identical to what
        the harness passes to the manager hook. Returns the same keys:
        active_stage, nodes, dependencies, ready, global_tombstones.

        If no stage is active, active_stage is None and all collections are empty.
        Uses a large token budget so it never raises BudgetExceeded — callers
        that need budget enforcement should call context.manager_view() directly.
        """
        from a7_rt_core.context.core import manager_view

        doc = self._load()
        return manager_view(doc, budget_tokens=10_000_000)

    # -------------------------------------------------------------------------
    # Manager lifecycle
    # -------------------------------------------------------------------------

    def manager_checkpoint(self, state: ManagerState) -> None:
        """
        Atomically write manager.json with the current ManagerState.

        Invariant: state.mode must not be DEAD — dead managers use manager_seal().
        Stamps checkpoint_at with the current UTC time.
        """
        if state.mode == ManagerMode.DEAD:
            raise InvariantViolation(
                "Cannot checkpoint a dead manager. Use manager_seal() to record the final state."
            )
        stamped_data = state.model_dump(mode="json")
        stamped_data["checkpoint_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write_json(self._manager_path, stamped_data)

    def manager_kill(self) -> None:
        """
        Remove manager.json.

        The absence of manager.json is the definitive signal that no manager
        is running. Idempotent — safe to call when manager.json is already absent.
        """
        try:
            os.unlink(self._manager_path)
        except FileNotFoundError:
            pass

    def manager_seal(self, state: ManagerState) -> None:
        """
        Execute the manager death contract.

        1. Stamp mode=DEAD and checkpoint_at onto the state.
        2. Write the dead state to manager.json (recoverable from events on crash).
        3. Append a 'death' event to events.jsonl.
        4. Remove manager.json — the manager is dead.

        Callers pass the manager's final ManagerState (any mode). This method
        always forces mode=DEAD regardless of the incoming mode.
        """
        now = datetime.now(timezone.utc).isoformat()
        dead_data = state.model_dump(mode="json")
        dead_data["mode"] = ManagerMode.DEAD.value
        dead_data["checkpoint_at"] = now

        # Step 2: write before appending event — if we crash here, the dead state
        # is on disk and the next operator can read it from manager.json.
        self._atomic_write_json(self._manager_path, dead_data)

        # Step 3: permanent record in the audit trail
        self.append_event(
            {
                "turn": state.turn,
                "timestamp": now,
                "actor": "manager",
                "action": "death",
                "target": state.current_stage_id,
                "detail": (
                    f"Manager lifecycle ended at turn {state.turn}/{state.manager_max_turns}"
                ),
                "state_delta": {
                    "manager_mode": f"{state.mode.value} → dead",
                    "in_flight": state.in_flight,
                    "pending_human_input": len(state.human_input_queue),
                },
            }
        )

        # Step 4: remove the liveness marker
        self.manager_kill()

    # -------------------------------------------------------------------------
    # Convenience helpers
    # -------------------------------------------------------------------------

    @property
    def manager_alive(self) -> bool:
        """True if manager.json exists (manager is currently running)."""
        return self._manager_path.exists()

    def load_manager_state(self) -> ManagerState | None:
        """Return the current ManagerState, or None if no manager is alive."""
        if not self._manager_path.exists():
            return None
        with open(self._manager_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Migration: stage_intent -> planning (if present but planning absent)
        if "stage_intent" in data and data.get("stage_intent") is not None:
            if "planning" not in data or data.get("planning") is None:
                from a7_rt_core.core.models import PlanningState

                data["planning"] = PlanningState(
                    narrative=data["stage_intent"],
                    checklist=[],
                    updated_at=data.get("turn", 0),
                ).model_dump()
            del data["stage_intent"]

        return ManagerState.model_validate(data)

    def load_project(self) -> Project:
        """Return the project config from master.json."""
        return Project.model_validate(self._load()["project"])

    # -------------------------------------------------------------------------
    # Session logging (for warm redispatch)
    # -------------------------------------------------------------------------

    def append_session_log(self, node_id: str, entry: dict) -> None:
        """Append single line to content/.sessions/{node_id}.jsonl"""
        session_path = self._sessions_dir / f"{node_id}.jsonl"
        line = json.dumps(entry, default=str)
        with open(session_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def get_sessions_for_node(self, node_id: str) -> list[AgentSession]:
        """Parse {node_id}.jsonl, return list of completed sessions."""
        session_path = self._sessions_dir / f"{node_id}.jsonl"
        if not session_path.exists():
            return []

        sessions: dict[str, dict] = {}
        for line in session_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = event.get("session_id")
            if not session_id:
                continue

            if session_id not in sessions:
                sessions[session_id] = {
                    "session_id": session_id,
                    "node_id": event.get("node_id", node_id),
                    "dispatch_turn": event.get("dispatch_turn", 0),
                    "role": event.get("role", "unknown"),
                    "attempt_number": event.get("attempt_number", 1),
                    "status": "running",
                    "iterations_used": 0,
                    "files_written": [],
                    "created_at": event.get("created_at", ""),
                    "ended_at": None,
                }

            if event.get("type") == "session_start":
                sessions[session_id].update(
                    {
                        "dispatch_turn": event.get("dispatch_turn", 0),
                        "role": event.get("role", "unknown"),
                        "attempt_number": event.get("attempt_number", 1),
                        "created_at": event.get("created_at", ""),
                    }
                )
            elif event.get("type") == "session_end":
                sessions[session_id].update(
                    {
                        "status": event.get("status", "completed"),
                        "iterations_used": event.get("iterations_used", 0),
                        "files_written": event.get("files_written", []),
                        "ended_at": event.get("ended_at"),
                    }
                )

        result = []
        for sess_data in sessions.values():
            try:
                result.append(AgentSession.model_validate(sess_data))
            except Exception:
                continue
        return result

    def get_active_session_for_node(self, node_id: str) -> AgentSession | None:
        """Return session with status 'running' or None."""
        sessions = self.get_sessions_for_node(node_id)
        for sess in sessions:
            if sess.status == "running":
                return sess
        return None

    def count_sessions_for_node(self, node_id: str) -> int:
        """Count total sessions for attempt_number calculation."""
        return len(self.get_sessions_for_node(node_id))

    def get_session_by_id(self, session_id: str) -> AgentSession | None:
        """Find session across all node session files (scans all files)."""
        if not self._sessions_dir.exists():
            return None

        for session_file in self._sessions_dir.glob("*.jsonl"):
            node_id = session_file.stem
            sessions = self.get_sessions_for_node(node_id)
            for sess in sessions:
                if sess.session_id == session_id:
                    return sess
        return None

    def get_exploration_hints(self, node_id: str) -> dict:
        """Get exploration hints for warm redispatch.

        Returns slim structure with only grounded outputs (files_written),
        not agent scratchpad (files read, reasoning trace).
        """
        sessions = self.get_sessions_for_node(node_id)

        if not sessions:
            return {
                "previous_attempts": 0,
                "files_written": [],
            }

        # Collect files_written from session results (grounded outputs only)
        files_written: set[str] = set()
        for sess in sessions:
            if sess.files_written:
                files_written.update(sess.files_written)

        return {
            "previous_attempts": len(sessions),
            "files_written": sorted(files_written),
        }
