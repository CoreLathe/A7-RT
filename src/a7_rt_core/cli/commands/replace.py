# cli/commands/replace.py — Replace a node with a new implementation

"""Replace a suspended/failed node with a new node.

Usage:
    python -m cli replace <session-path> <old-node> <new-node> [options]

Options:
    --redirect-deps        Update all nodes that depend on old-node to point to new-node
    --copy-interface       Copy interface.exports from old-node to new-node
    --dry-run              Show what would change without applying
    --force                Allow replacement even if old-node is not suspended/poisoned
    --help, -h             Show this help message

Examples:
    # Replace suspended admin.api with admin.api.v2
    python -m cli replace /tmp/session admin.api admin.api.v2 --redirect-deps

    # Dry run to see what would change
    python -m cli replace /tmp/session old.node new.node --dry-run

    # Copy interface and redirect dependencies
    python -m cli replace /tmp/session auth.v1 auth.v2 --copy-interface --redirect-deps
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def run_replace(argv: list[str]) -> int:
    """Run the replace command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path_str = args.get("session_path")
    old_node_id = args.get("old_node")
    new_node_id = args.get("new_node")

    if not session_path_str or not old_node_id or not new_node_id:
        print("error: session path, old-node, and new-node are required", file=sys.stderr)
        print("Usage: python -m cli replace <session-path> <old-node> <new-node>", file=sys.stderr)
        return 1

    session_path = Path(session_path_str).resolve()
    master_path = session_path / "master.json"

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

    nodes = master.get("nodes", {})
    dependencies = master.get("dependencies", [])

    # Validate old node exists
    if old_node_id not in nodes:
        print(f"error: old node '{old_node_id}' not found", file=sys.stderr)
        return 1

    # Validate new node exists
    if new_node_id not in nodes:
        print(f"error: new node '{new_node_id}' not found", file=sys.stderr)
        print(f"hint: use 'python -m cli seed' to add '{new_node_id}' first", file=sys.stderr)
        return 1

    old_node = nodes[old_node_id]
    new_node = nodes[new_node_id]

    # Safety check: old node should be suspended or poisoned (unless --force)
    if not args.get("force"):
        old_status = old_node.get("status", "")
        if old_status not in ("suspended", "poisoned"):
            print(f"error: old node '{old_node_id}' has status '{old_status}'", file=sys.stderr)
            print("hint: use --force to replace anyway, or suspend the node first", file=sys.stderr)
            return 1

    # Find dependent nodes (nodes that have old_node as a dependency)
    dependent_nodes = []
    for dep in dependencies:
        if dep.get("to_node") == old_node_id:
            dependent_nodes.append(dep.get("from_node"))

    # Build plan
    plan = {
        "old_node": old_node_id,
        "new_node": new_node_id,
        "redirect_deps": args.get("redirect_deps", False),
        "copy_interface": args.get("copy_interface", False),
        "dependencies_to_update": dependent_nodes,
    }

    # Dry run: show plan and exit
    if args.get("dry_run"):
        print("DRY RUN — would perform the following changes:")
        print(f"  - Old node: {old_node_id} (status: {old_node.get('status')})")
        print(f"  - New node: {new_node_id} (status: {new_node.get('status')})")
        if plan["copy_interface"]:
            old_exports = old_node.get("interface", {}).get("exports", [])
            print(f"  - Copy interface.exports: {old_exports}")
        if plan["redirect_deps"] and dependent_nodes:
            print(f"  - Update dependencies for: {', '.join(dependent_nodes)}")
            print(f"    (redirect from {old_node_id} -> {new_node_id})")
        elif dependent_nodes:
            print(f"  - WARNING: {len(dependent_nodes)} nodes depend on {old_node_id}")
            print(f"    Use --redirect-deps to update: {', '.join(dependent_nodes)}")
        return 0

    # Apply changes
    changes_made = []

    # 1. Copy interface if requested
    if args.get("copy_interface"):
        old_interface = old_node.get("interface", {})
        if old_interface:
            new_node["interface"] = old_interface.copy()
            changes_made.append(f"Copied interface from {old_node_id} to {new_node_id}")

    # 2. Redirect dependencies if requested
    if args.get("redirect_deps"):
        updated_count = 0
        for dep in dependencies:
            if dep.get("to_node") == old_node_id:
                dep["to_node"] = new_node_id
                dep["redirected_from"] = old_node_id  # Audit trail
                updated_count += 1

        # Also update structural_deps in dependent nodes
        for dep_nid in dependent_nodes:
            dep_node = nodes.get(dep_nid)
            if dep_node:
                struct_deps = dep_node.get("structural_deps", [])
                if old_node_id in struct_deps:
                    struct_deps[struct_deps.index(old_node_id)] = new_node_id
                    dep_node["structural_deps"] = struct_deps
                    updated_count += 1

        if updated_count > 0:
            changes_made.append(f"Updated {updated_count} dependencies to point to {new_node_id}")

    # 3. Mark old node as replaced (add to metadata)
    if "metadata" not in old_node:
        old_node["metadata"] = {}
    if isinstance(old_node["metadata"], dict):
        old_node["metadata"]["replaced_by"] = new_node_id
        old_node["metadata"]["replaced_at"] = _now_iso()
        changes_made.append(f"Marked {old_node_id} as replaced by {new_node_id}")

    # 4. Move to graveyard (optional — keeps active nodes clean)
    # Only if the node is not in a sealed stage
    old_stage_id = old_node.get("stage_id")
    stages = master.get("stages", {})
    old_stage = stages.get(old_stage_id, {})
    if old_stage.get("status") != "sealed":
        graveyard = master.setdefault("graveyard", [])
        graveyard_entry = {
            "node_id": old_node_id,
            "reason": f"replaced_by:{new_node_id}",
            "scope": old_stage_id,
            "timestamp": _now_iso(),
        }
        if graveyard_entry not in graveyard:
            graveyard.append(graveyard_entry)
            changes_made.append(f"Added {old_node_id} to graveyard")

    # Save master
    try:
        with open(master_path, "w") as f:
            json.dump(master, f, indent=2)
    except Exception as exc:
        print(f"error: cannot write master.json: {exc}", file=sys.stderr)
        return 1

    # Report
    print(f"Replaced {old_node_id} -> {new_node_id}")
    for change in changes_made:
        print(f"  ✓ {change}")

    if dependent_nodes and not args.get("redirect_deps"):
        print(f"\nWARNING: {len(dependent_nodes)} nodes still depend on {old_node_id}:")
        for dep_nid in dependent_nodes:
            print(f"  - {dep_nid}")
        print(f"Run with --redirect-deps to update their dependencies.")

    return 0


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse replace command arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "old_node": None,
        "new_node": None,
        "redirect_deps": False,
        "copy_interface": False,
        "dry_run": False,
        "force": False,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg == "--redirect-deps":
            args["redirect_deps"] = True

        elif arg == "--copy-interface":
            args["copy_interface"] = True

        elif arg == "--dry-run":
            args["dry_run"] = True

        elif arg == "--force":
            args["force"] = True

        elif not arg.startswith("-"):
            # Positional arguments
            if args["session_path"] is None:
                args["session_path"] = arg
            elif args["old_node"] is None:
                args["old_node"] = arg
            elif args["new_node"] is None:
                args["new_node"] = arg
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
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_HELP = """
Usage: python -m cli replace <session-path> <old-node> <new-node> [options]

Replace a suspended/failed node with a new node.

Arguments:
  session-path             Path to A7-RT session (required)
  old-node                 Node ID to replace (must be suspended/poisoned)
  new-node                 Node ID to use as replacement (must exist)

Options:
  --redirect-deps          Update all dependencies pointing to old-node
  --copy-interface         Copy interface.exports from old to new
  --dry-run                Show what would change without applying
  --force                  Allow replacement even if old-node is active
  --help, -h               Show this help message

Examples:
  # Replace suspended admin.api with admin.api.v2
  python -m cli replace /tmp/session admin.api admin.api.v2 --redirect-deps

  # Preview changes
  python -m cli replace /tmp/session auth.v1 auth.v2 --dry-run

  # Copy interface and redirect all dependencies
  python -m cli replace /tmp/session old.node new.node --copy-interface --redirect-deps
"""
