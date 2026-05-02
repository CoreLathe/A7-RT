"""Stage reset command — reset an active stage to start fresh.

Usage:
    python -m cli stage-reset <session-path> <stage-id> [options]

Options:
    --delete-content         Delete content files (default: archive to .archive/)
    --preserve-pattern=<re>  Preserve nodes matching regex (default: .*\\.v2)
    --force                  Allow reset even if stage has dependent stages
    --dry-run                Show what would change without applying
    --help, -h               Show this help message

Examples:
    # Reset stage-2, archive content, preserve .v2 nodes
    python -m cli stage-reset /tmp/session stage-2

    # Nuclear reset — delete all content
    python -m cli stage-reset /tmp/session stage-2 --delete-content

    # Dry run to see what would happen
    python -m cli stage-reset /tmp/session stage-2 --dry-run
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def run_stage_reset(argv: list[str]) -> int:
    """Run the stage-reset command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path_str = args.get("session_path")
    stage_id = args.get("stage_id")

    if not session_path_str or not stage_id:
        print("error: session path and stage-id are required", file=sys.stderr)
        print("Usage: python -m cli stage-reset <session-path> <stage-id>", file=sys.stderr)
        return 1

    session_path = Path(session_path_str).resolve()
    master_path = session_path / "master.json"
    content_path = session_path / "content"

    if not master_path.exists():
        print(f"error: no session found at {session_path}", file=sys.stderr)
        return 1

    # Load master
    try:
        with open(master_path) as f:
            master = json.load(f)
    except Exception as exc:
        print(f"error: cannot read master.json: {exc}", file=sys.stderr)
        return 1

    stages = master.get("stages", {})
    nodes = master.get("nodes", {})
    dependencies = master.get("dependencies", [])

    # Validate stage exists
    if stage_id not in stages:
        print(f"error: stage '{stage_id}' not found", file=sys.stderr)
        print(f"  available stages: {', '.join(stages.keys())}", file=sys.stderr)
        return 1

    stage = stages[stage_id]
    stage_status = stage.get("status", "unknown")

    # Check if stage is sealed
    if stage_status == "sealed":
        print(f"error: cannot reset sealed stage '{stage_id}'", file=sys.stderr)
        print("  sealed stages are immutable ratchets", file=sys.stderr)
        return 1

    # Check if this is the last active stage (no dependent stages)
    dependent_stages = _find_dependent_stages(stage_id, stages, nodes, dependencies)
    if dependent_stages and not args.get("force"):
        print(
            f"error: stage '{stage_id}' has dependent stages: {', '.join(dependent_stages)}",
            file=sys.stderr,
        )
        print("  reset would orphan downstream work", file=sys.stderr)
        print("  use --force to override (dangerous)", file=sys.stderr)
        return 1

    # Get nodes in this stage
    stage_node_ids = stage.get("node_ids", [])
    preserve_pattern = args.get("preserve_pattern", r".*\.v2$")
    preserve_re = re.compile(preserve_pattern)

    # Separate nodes to preserve vs remove
    nodes_to_preserve = []
    nodes_to_remove = []

    for nid in stage_node_ids:
        if preserve_re.match(nid):
            nodes_to_preserve.append(nid)
        else:
            nodes_to_remove.append(nid)

    # Build plan
    plan = {
        "stage_id": stage_id,
        "delete_content": args.get("delete_content", False),
        "preserve_pattern": preserve_pattern,
        "nodes_to_preserve": nodes_to_preserve,
        "nodes_to_remove": nodes_to_remove,
        "dependent_stages": dependent_stages,
    }

    # Dry run: show plan and exit
    if args.get("dry_run"):
        print("DRY RUN — would perform the following changes:")
        print(f"  Stage: {stage_id} (status: {stage_status})")
        print(f"  Preserve pattern: {preserve_pattern}")
        print(f"  Nodes to preserve: {len(nodes_to_preserve)}")
        for nid in nodes_to_preserve:
            print(f"    - {nid}")
        print(f"  Nodes to remove: {len(nodes_to_remove)}")
        for nid in nodes_to_remove:
            print(f"    - {nid}")
        if plan["delete_content"]:
            print("  Content files: DELETE")
        else:
            archive_dir = content_path / ".archive" / f"{stage_id}-{_timestamp()}"
            print(f"  Content files: ARCHIVE to {archive_dir}")
        if dependent_stages:
            print(f"  WARNING: dependent stages exist: {', '.join(dependent_stages)}")
        return 0

    # Apply changes
    changes_made = []

    # 1. Archive or delete content files
    delete_content = args.get("delete_content", False)
    archive_dir = None

    if nodes_to_remove:
        if delete_content:
            # Delete content files
            deleted_count = 0
            for nid in nodes_to_remove:
                node = nodes.get(nid, {})
                content_file = node.get("content_file")
                if content_file:
                    file_path = content_path / content_file
                    if file_path.exists():
                        file_path.unlink()
                        deleted_count += 1
                # Also delete session logs
                session_log = content_path / ".sessions" / f"{nid}.jsonl"
                if session_log.exists():
                    session_log.unlink()
            changes_made.append(f"Deleted {deleted_count} content files")
        else:
            # Archive content files
            archive_dir = content_path / ".archive" / f"{stage_id}-{_timestamp()}"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archived_count = 0
            for nid in nodes_to_remove:
                node = nodes.get(nid, {})
                content_file = node.get("content_file")
                if content_file:
                    src = content_path / content_file
                    if src.exists():
                        dst = archive_dir / src.name
                        shutil.move(str(src), str(dst))
                        archived_count += 1
                # Archive session logs
                session_log = content_path / ".sessions" / f"{nid}.jsonl"
                if session_log.exists():
                    dst = archive_dir / f"{nid}.jsonl"
                    shutil.move(str(session_log), str(dst))
            changes_made.append(f"Archived {archived_count} files to {archive_dir}")

    # 2. Move nodes to graveyard
    graveyard = master.setdefault("graveyard", [])
    for nid in nodes_to_remove:
        graveyard_entry = {
            "node_id": nid,
            "reason": f"stage_reset:{stage_id}",
            "scope": stage_id,
            "timestamp": _now_iso(),
        }
        # Avoid duplicates
        if not any(
            g.get("node_id") == nid and g.get("reason", "").startswith("stage_reset")
            for g in graveyard
        ):
            graveyard.append(graveyard_entry)

    if nodes_to_remove:
        changes_made.append(f"Moved {len(nodes_to_remove)} nodes to graveyard")

    # 3. Remove node entries from master.json
    for nid in nodes_to_remove:
        if nid in nodes:
            del nodes[nid]

    # 4. Remove dependency edges for removed nodes
    master["dependencies"] = [
        dep
        for dep in dependencies
        if dep.get("from_node") not in nodes_to_remove and dep.get("to_node") not in nodes_to_remove
    ]

    # 5. Update stage node_ids to only preserved nodes
    stage["node_ids"] = nodes_to_preserve
    stage["status"] = "active"  # Reset to active (even if it was suspended/poisoned)

    changes_made.append(f"Stage '{stage_id}' reset with {len(nodes_to_preserve)} preserved nodes")

    # Save master
    try:
        with open(master_path, "w") as f:
            json.dump(master, f, indent=2)
    except Exception as exc:
        print(f"error: cannot write master.json: {exc}", file=sys.stderr)
        return 1

    # Report
    print(f"Reset stage: {stage_id}")
    for change in changes_made:
        print(f"  ✓ {change}")

    if nodes_to_preserve:
        print(f"\nPreserved nodes ({len(nodes_to_preserve)}):")
        for nid in nodes_to_preserve:
            print(f"  - {nid}")

    if not delete_content and archive_dir:
        print(f"\nArchive location: {archive_dir}")
        print("  (use --delete-content to remove instead of archive)")

    print(f"\nStage '{stage_id}' is ready for fresh seeding.")
    print(f"  Run: python -m cli seed {session_path} --spec=...")

    return 0


def _find_dependent_stages(
    stage_id: str, stages: dict, nodes: dict, dependencies: list
) -> list[str]:
    """Find stages that depend on nodes in the given stage."""
    # Get all nodes in the target stage
    stage_node_ids = set(stages.get(stage_id, {}).get("node_ids", []))

    # Find all nodes that depend on these nodes
    dependent_nodes = set()
    for dep in dependencies:
        if dep.get("to_node") in stage_node_ids:
            dependent_nodes.add(dep.get("from_node"))

    # Find which stages those nodes belong to
    dependent_stages = set()
    for nid in dependent_nodes:
        node = nodes.get(nid, {})
        node_stage = node.get("stage_id")
        if node_stage and node_stage != stage_id:
            dependent_stages.add(node_stage)

    return sorted(dependent_stages)


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse stage-reset command arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "stage_id": None,
        "delete_content": False,
        "preserve_pattern": r".*\.v2$",
        "force": False,
        "dry_run": False,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg == "--delete-content":
            args["delete_content"] = True

        elif arg.startswith("--preserve-pattern="):
            args["preserve_pattern"] = arg.split("=", 1)[1]

        elif arg == "--force":
            args["force"] = True

        elif arg == "--dry-run":
            args["dry_run"] = True

        elif not arg.startswith("-"):
            # Positional arguments
            if args["session_path"] is None:
                args["session_path"] = arg
            elif args["stage_id"] is None:
                args["stage_id"] = arg
            else:
                print(f"error: unexpected argument: {arg}", file=sys.stderr)
                sys.exit(1)

        else:
            print(f"error: unknown option: {arg}", file=sys.stderr)
            sys.exit(1)

        i += 1

    return args


def _now_iso() -> str:
    """Return current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    """Return compact timestamp for directory names."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


_HELP = """
Usage: python -m cli stage-reset <session-path> <stage-id> [options]

Reset an active stage to start fresh. Removes all nodes in the stage
(except those matching --preserve-pattern), moves them to graveyard,
and archives or deletes their content files.

This is safe ONLY for the last active stage — sealed upstream stages
are immutable and unaffected.

Arguments:
  session-path             Path to A7-RT session (required)
  stage-id                 Stage to reset (required)

Options:
  --delete-content         Delete content files (default: archive to .archive/)
  --preserve-pattern=<re>  Regex to preserve matching nodes (default: .*\\.v2$)
  --force                  Allow reset even if stage has dependent stages
  --dry-run                Show what would change without applying
  --help, -h               Show this help message

Examples:
  # Reset stage-2, archive content, preserve .v2 nodes
  python -m cli stage-reset /tmp/session stage-2

  # Reset without preserving any nodes
  python -m cli stage-reset /tmp/session stage-2 --preserve-pattern='$^'

  # Nuclear option — delete all content
  python -m cli stage-reset /tmp/session stage-2 --delete-content

  # Dry run to preview changes
  python -m cli stage-reset /tmp/session stage-2 --dry-run

Safety:
  - Cannot reset sealed stages (immutable by design)
  - Refuses to reset if dependent stages exist (use --force to override)
  - Content files are archived by default (not deleted)
"""
