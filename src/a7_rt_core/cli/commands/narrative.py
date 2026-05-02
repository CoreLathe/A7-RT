# cli/commands/narrative.py — View session narrative

"""Reconstruct and display the narrative of an A7-RT session.

Usage:
    python -m cli narrative <session-path> [options]

Options:
    --max-turns=<N>          Limit to first N turns
    --node=<node_id>         Filter to specific node
    --raw                    Output raw events (JSONL)

Examples:
    python -m cli narrative /tmp/my-session
    python -m cli narrative /tmp/my-session --max-turns=20
    python -m cli narrative /tmp/my-session --node=auth.handler
    python -m cli narrative /tmp/my-session --raw > events.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def run_narrative(argv: list[str]) -> int:
    """Run the narrative command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path_str = args.get("session_path")
    if not session_path_str:
        print("error: session path is required", file=sys.stderr)
        print("Usage: python -m cli narrative <session-path>", file=sys.stderr)
        return 1

    session_path = Path(session_path_str).resolve()

    # Validate session
    master_path = session_path / "master.json"
    events_path = session_path / "events.jsonl"

    if not master_path.exists():
        print(f"error: not a valid session: {session_path}", file=sys.stderr)
        return 1

    # Load master for context
    try:
        with open(master_path) as f:
            master = json.load(f)
    except Exception as exc:
        print(f"error: cannot read master.json: {exc}", file=sys.stderr)
        return 1

    # Load events
    events = _load_jsonl(events_path)

    if not events:
        print(f"No events found in session: {session_path}")
        return 0

    # Apply filters
    event_limit = args.get("max_turns")
    node_filter = args.get("node")

    if event_limit:
        events = events[:event_limit]

    if node_filter:
        events = [
            e for e in events if e.get("target") == node_filter or e.get("node_id") == node_filter
        ]

    # Output mode
    if args.get("raw"):
        # Raw JSONL output
        for event in events:
            print(json.dumps(event))
        return 0

    # Formatted narrative output
    _print_narrative(events, master, session_path)
    return 0


def _load_jsonl(path: Path) -> list[dict]:
    """Load JSON lines file, skipping invalid lines."""
    entries = []
    if not path.exists():
        return entries

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _print_narrative(events: list[dict], master: dict, session_path: Path) -> None:
    """Print formatted narrative of session events."""
    project = master.get("project", {})
    nodes = master.get("nodes", {})

    print("=" * 70)
    print(f"Session Narrative: {project.get('name', session_path.name)}")
    print(f"Path: {session_path}")
    print(f"Created: {project.get('created_at', 'unknown')}")
    print(f"Max Turns: {project.get('manager_max_turns', 'N/A')}")
    print("=" * 70)
    print()

    # Group events by turn
    by_turn: dict[int, list[dict]] = defaultdict(list)
    for event in events:
        turn = event.get("turn", 0)
        by_turn[turn].append(event)

    # Process each turn
    for turn in sorted(by_turn.keys()):
        turn_events = by_turn[turn]
        print(f"\n{'─' * 70}")
        print(f"Turn {turn}")
        print(f"{'─' * 70}")

        for event in turn_events:
            _print_event(event, nodes)

    # Summary
    print()
    print("=" * 70)
    print(f"Total events: {len(events)}")
    print(f"Turns covered: {len(by_turn)}")
    grounded = sum(1 for n in nodes.values() if n.get("status") == "grounded")
    total = len(nodes)
    print(f"Nodes grounded: {grounded}/{total}")
    print("=" * 70)


def _print_event(event: dict, nodes: dict) -> None:
    """Print a single event in narrative form."""
    action = event.get("action", "unknown")
    actor = event.get("actor", "system")
    target = event.get("target") or event.get("node_id")
    timestamp = event.get("timestamp", "")
    detail = event.get("detail", "")

    # Format timestamp
    time_str = ""
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            time_str = dt.strftime("%H:%M:%S")
        except ValueError:
            pass

    prefix = f"[{time_str}] " if time_str else ""

    if action == "dispatch":
        print(f"\n{prefix}→ DISPATCH to {target}")
        intent = event.get("intent", "")
        if intent:
            print(f"    Intent: {intent}")

    elif action == "return":
        status = event.get("status", "unknown")
        print(f"\n{prefix}← RETURN from {target} [{status}]")
        if detail:
            print(f"    Detail: {detail}")

    elif action == "commit":
        print(f"\n{prefix}✓ COMMIT {target}")
        if detail:
            print(f"    {detail}")

    elif action == "suspend":
        print(f"\n{prefix}⏸ SUSPEND {target}")
        if detail:
            print(f"    Reason: {detail}")

    elif action == "poison":
        print(f"\n{prefix}☠ POISON {target}")
        if detail:
            print(f"    Cascade: {detail}")

    elif action == "seal":
        stage = event.get("stage_id", "unknown")
        print(f"\n{prefix}🔒 SEAL stage {stage}")

    elif action == "human_input":
        print(f"\n{prefix}👤 HUMAN INPUT requested")
        if detail:
            print(f"    {detail}")

    else:
        # Generic event
        print(f"\n{prefix}• {action.upper()}")
        if target:
            print(f"    Target: {target}")
        if detail:
            print(f"    Detail: {detail}")


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse narrative command arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "max_turns": None,
        "node": None,
        "raw": False,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg.startswith("--max-turns="):
            try:
                args["max_turns"] = int(arg.split("=", 1)[1])
            except ValueError:
                print("error: --max-turns requires an integer", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--node="):
            args["node"] = arg.split("=", 1)[1]

        elif arg == "--raw":
            args["raw"] = True

        elif not arg.startswith("-"):
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
Usage: python -m cli narrative <session-path> [options]

Reconstruct the narrative of an A7-RT session — what the manager saw,
what it decided, and how subagents responded.

Arguments:
  session-path             Path to A7-RT session (required)

Options:
  --max-turns=<N>          Limit display to first N turns
  --node=<node_id>         Filter narrative to specific node only
  --raw                    Output raw events (JSONL) instead of formatted
  --help, -h               Show this help message

Examples:
  python -m cli narrative /tmp/my-session
  python -m cli narrative /tmp/my-session --max-turns=20
  python -m cli narrative /tmp/my-session --node=auth.handler
  python -m cli narrative /tmp/my-session --raw > events.jsonl
"""
