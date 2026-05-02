# cli/commands/init.py — Initialize a new A7-RT session

"""Initialize a new A7-RT session.

Usage:
    python -m cli init <session-path> [options]

Options:
    --source=<path>          Source directory to scan for nodes
    --dry-run                Print plan without creating files
    --name=<name>            Session name (default: directory name)
    --manager-max-turns=<N>  Default max turns (default: 50)
    --drain-turn=<N>         Default drain turn (default: 40)

Examples:
    python -m cli init /tmp/my-session
    python -m cli init /tmp/my-session --source=./src --dry-run
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Config creation is now isolated to `a7-rt config --init`
# Sessions inherit from user/project config hierarchy without forced local .a7


def run_init(argv: list[str]) -> int:
    """Run the init command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path_str = args.get("session_path")
    if not session_path_str:
        print("error: session path is required", file=sys.stderr)
        print("Usage: python -m cli init <session-path>", file=sys.stderr)
        return 1

    session_path = Path(session_path_str).resolve()

    # Dry run: just show what would be created
    if args.get("dry_run"):
        print(f"Would create session at: {session_path}")
        print(f"  - master.json")
        print(f"  - content/ directory")
        print(f"  - events.jsonl")
        if args.get("source"):
            print(f"  - Scan source: {args['source']}")
        return 0

    # Create session directory
    try:
        session_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"error: cannot create session directory: {exc}", file=sys.stderr)
        return 1

    # Create master.json
    project_id = str(session_path.name)
    master_data = {
        "project": {
            "project_id": project_id,
            "name": args.get("name") or session_path.name,
            "description": f"A7-RT session: {session_path.name}",
            "created_at": _now_iso(),
            "manager_max_turns": args.get("manager_max_turns", 50),
            "drain_turn": args.get("drain_turn", 40),
            "provisional_depth_limit": 3,
        },
        "stages": {
            "stage-1": {
                "stage_id": "stage-1",
                "project_id": project_id,
                "name": "stage-1",
                "status": "active",
                "created_at": _now_iso(),
                "node_ids": [],
            }
        },
        "nodes": {},
        "dependencies": [],
        "graveyard": [],
    }

    master_path = session_path / "master.json"
    try:
        with open(master_path, "w") as f:
            json.dump(master_data, f, indent=2)
    except Exception as exc:
        print(f"error: failed to write master.json: {exc}", file=sys.stderr)
        return 1

    # Create empty events.jsonl
    events_path = session_path / "events.jsonl"
    try:
        events_path.touch()
    except Exception as exc:
        print(f"error: failed to create events.jsonl: {exc}", file=sys.stderr)
        return 1

    # Create content directory
    content_path = session_path / "content"
    try:
        content_path.mkdir(exist_ok=True)
    except Exception as exc:
        print(f"error: failed to create content directory: {exc}", file=sys.stderr)
        return 1

    # Create .sessions directory for per-node session logs
    sessions_path = content_path / ".sessions"
    try:
        sessions_path.mkdir(exist_ok=True)
    except Exception as exc:
        print(f"error: failed to create .sessions directory: {exc}", file=sys.stderr)
        return 1

    print(f"Created A7-RT session: {session_path}")
    print(f"  master.json: {master_path}")
    print(f"  content/: {content_path}")
    print(f"  content/.sessions/: {sessions_path}")
    print(f"  events.jsonl: {events_path}")

    # Show config inheritance info
    print(f"\nConfig: Uses ~/.a7/config.toml (create with: a7-rt config --init)")
    print(f"        Project .a7/config.toml (if within project with .a7/)")
    print(f"        Session .a7/config.toml (create manually if needed)")

    if args.get("source"):
        print(
            f"\nTo import from source: python -m cli seed {session_path} --source={args['source']}"
        )

    return 0


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse init command arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "source": None,
        "dry_run": False,
        "name": None,
        "manager_max_turns": 50,
        "drain_turn": 40,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg == "--dry-run":
            args["dry_run"] = True

        elif arg.startswith("--source="):
            args["source"] = arg.split("=", 1)[1]

        elif arg.startswith("--name="):
            args["name"] = arg.split("=", 1)[1]

        elif arg.startswith("--manager-max-turns="):
            try:
                args["manager_max_turns"] = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"error: --manager-max-turns requires an integer", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--drain-turn="):
            try:
                args["drain_turn"] = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"error: --drain-turn requires an integer", file=sys.stderr)
                sys.exit(1)

        elif not arg.startswith("-"):
            # Positional argument: session path
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


def _now_iso() -> str:
    """Return current timestamp in ISO format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_HELP = """
Usage: python -m cli init <session-path> [options]

Initialize a new A7-RT session directory with master.json and content structure.

Arguments:
  session-path             Path where session will be created (required)

Options:
  --source=<path>          Source directory to scan for nodes (optional)
  --dry-run                Show what would be created without writing files
  --name=<name>            Session name (default: directory name)
  --manager-max-turns=<N>  Default max turns (default: 50)
  --drain-turn=<N>         Default drain turn (default: 40)
  --help, -h               Show this help message

Examples:
  python -m cli init /tmp/my-session
  python -m cli init /tmp/my-session --name="My Project"
  python -m cli init /tmp/my-session --dry-run
"""
