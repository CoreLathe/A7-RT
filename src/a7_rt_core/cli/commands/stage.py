# cli/commands/stage.py — Create a new stage in an A7-RT session

"""Create a new stage in an existing A7-RT session.

Usage:
    python -m cli stage <session-path> [options]

Options:
    --name=<name>            Stage name (default: stage-N)
    --stage-id=<id>          Explicit stage ID (default: auto-generated)

Examples:
    python -m cli stage /tmp/my-session
    python -m cli stage /tmp/my-session --name=api --stage-id=api-stage
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any


def run_stage_create(argv: list[str]) -> int:
    """Run the stage-create command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path_str = args.get("session_path")
    if not session_path_str:
        print("error: session path is required", file=sys.stderr)
        print("Usage: python -m cli stage <session-path>", file=sys.stderr)
        return 1

    session_path = Path(session_path_str).resolve()

    # Validate session exists
    master_path = session_path / "master.json"
    if not master_path.exists():
        print(f"error: not a valid session: {session_path}", file=sys.stderr)
        print("  (master.json not found)", file=sys.stderr)
        return 1

    # Load master.json
    try:
        with open(master_path) as f:
            master = json.load(f)
    except Exception as exc:
        print(f"error: failed to read master.json: {exc}", file=sys.stderr)
        return 1

    # Generate stage ID if not provided
    stage_id = args.get("stage_id")
    if not stage_id:
        # Find next stage number
        existing_stages = list(master.get("stages", {}).keys())
        stage_num = len(existing_stages) + 1
        stage_id = f"stage-{stage_num}"

    # Validate stage ID format (alphanumeric, hyphens, underscores)
    if not re.match(r"^[a-zA-Z0-9_-]+$", stage_id):
        print(f"error: invalid stage-id: {stage_id}", file=sys.stderr)
        print("  (use only letters, numbers, hyphens, underscores)", file=sys.stderr)
        return 1

    # Check for duplicate stage ID
    if stage_id in master.get("stages", {}):
        print(f"error: stage already exists: {stage_id}", file=sys.stderr)
        return 1

    # Determine stage name
    stage_name = args.get("name") or stage_id

    # Create stage entry
    from datetime import datetime, timezone

    # Get project_id from master
    project_id = master.get("project", {}).get("project_id", "unknown")

    stage_data = {
        "stage_id": stage_id,
        "project_id": project_id,
        "name": stage_name,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "node_ids": [],
    }

    # Add to master
    if "stages" not in master:
        master["stages"] = {}

    master["stages"][stage_id] = stage_data

    # Write back
    try:
        with open(master_path, "w") as f:
            json.dump(master, f, indent=2)
    except Exception as exc:
        print(f"error: failed to write master.json: {exc}", file=sys.stderr)
        return 1

    print(f"Created stage: {stage_id}")
    print(f"  name: {stage_name}")
    print(f"  session: {session_path}")

    return 0


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse stage command arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "name": None,
        "stage_id": None,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg.startswith("--name="):
            args["name"] = arg.split("=", 1)[1]

        elif arg.startswith("--stage-id="):
            args["stage_id"] = arg.split("=", 1)[1]

        elif not arg.startswith("-"):
            # Positional: session path
            if args["session_path"] is None:
                args["session_path"] = arg
            else:
                print(f"error: unexpected argument: {arg}", file=sys.stderr)
                sys.exit(1)

        else:
            print(f"error: unknown option: {arg}", file=sys.stderr)
            sys.exit(1)

        i += 1

    return args


_HELP = """
Usage: python -m cli stage <session-path> [options]

Create a new stage in an existing A7-RT session.

Arguments:
  session-path             Path to A7-RT session (must contain master.json)

Options:
  --name=<name>            Stage name (default: stage-ID)
  --stage-id=<id>          Explicit stage ID (default: auto-generated)
  --help, -h               Show this help message

Examples:
  python -m cli stage /tmp/my-session
  python -m cli stage /tmp/my-session --name=api
  python -m cli stage /tmp/my-session --stage-id=api-stage --name="API Layer"
"""
