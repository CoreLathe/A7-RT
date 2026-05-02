# cli/commands/run.py — CLI run command that delegates to TUI runner

"""Run an A7-RT session headlessly.

This module delegates to the TUI's run_headless implementation,
which is the working harness runner. The TUI runner has no Textual
dependencies for the 'run' subcommand.

Usage:
    python -m cli run <session-path> [options]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def run_headless(argv: list[str]) -> int:
    """
    Run the headless execution command.

    Delegates to tui.run_headless after adapting CLI arguments.
    """
    # Parse our simplified CLI args
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path = args.get("session_path")
    if not session_path:
        print("error: session path is required", file=sys.stderr)
        print("Usage: python -m cli run <session-path>", file=sys.stderr)
        return 1

    # Validate session exists
    master_path = Path(session_path) / "master.json"
    if not master_path.exists():
        print(f"error: no session found at {session_path}", file=sys.stderr)
        print("Run 'python -m cli init' first.", file=sys.stderr)
        return 1

    # Build argv for TUI runner
    # TUI expects: run <session-path> --stage-id=<id> [options]
    tui_argv = [session_path]

    if args.get("stage_id"):
        tui_argv.append(f"--stage-id={args['stage_id']}")

    if args.get("runner_turn_limit"):
        tui_argv.append(f"--turn-limit={args['runner_turn_limit']}")

    if args.get("context_mode"):
        tui_argv.append(f"--context-mode={args['context_mode']}")

    if args.get("model"):
        tui_argv.append(f"--model={args['model']}")

    if args.get("subagent_model"):
        tui_argv.append(f"--subagent-model={args['subagent_model']}")

    if args.get("daemon"):
        # TUI doesn't have daemon, but we can simulate with high turn count
        tui_argv.append("--turns=0")  # 0 = unlimited in TUI

    if args.get("emit_board"):
        tui_argv.append("--emit-board")

    if args.get("emit_raw"):
        tui_argv.append("--emit-raw")

    if args.get("emit_subagent_raw"):
        tui_argv.append("--emit-subagent-raw")

    if args.get("no_consult"):
        tui_argv.append("--no-consult")

    # Import and delegate to TUI runner
    # The TUI runner handles sys.exit() itself, so we need to catch that
    try:
        # Ensure a7-rt-core is on path
        core_dir = Path(__file__).resolve().parent.parent.parent
        if str(core_dir) not in sys.path:
            sys.path.insert(0, str(core_dir))

        from cli.runner import run_headless as cli_run_headless

        # TUI runner calls sys.exit() internally, so we need to catch it
        try:
            cli_run_headless(tui_argv)
            return 0  # Should not reach here due to sys.exit()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 0

    except ImportError as exc:
        print(f"error: failed to import TUI runner: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: execution failed: {exc}", file=sys.stderr)
        return 1


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse CLI arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "stage_id": None,
        "runner_turn_limit": 200,
        "context_mode": "accumulate",  # 'fresh' (experimental) or 'accumulate' (default)
        "daemon": False,
        "model": None,
        "subagent_model": None,
        "emit_board": False,
        "emit_raw": False,
        "emit_subagent_raw": False,
        "stdin_input": False,
        "no_consult": False,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg == "--daemon":
            args["daemon"] = True

        elif arg == "--no-consult":
            args["no_consult"] = True

        elif arg.startswith("--stage-id="):
            args["stage_id"] = arg.split("=", 1)[1]

        elif arg == "--stage-id":
            if i + 1 < len(argv):
                i += 1
                args["stage_id"] = argv[i]
            else:
                print("error: --stage-id requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--turn-limit="):
            try:
                args["runner_turn_limit"] = int(arg.split("=", 1)[1])
            except ValueError:
                print("error: --turn-limit requires an integer", file=sys.stderr)
                sys.exit(1)

        elif arg == "--turn-limit":
            if i + 1 < len(argv):
                i += 1
                try:
                    args["runner_turn_limit"] = int(argv[i])
                except ValueError:
                    print("error: --turn-limit requires an integer", file=sys.stderr)
                    sys.exit(1)
            else:
                print("error: --turn-limit requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--model="):
            args["model"] = arg.split("=", 1)[1]

        elif arg == "--model":
            if i + 1 < len(argv):
                i += 1
                args["model"] = argv[i]
            else:
                print("error: --model requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--subagent-model="):
            args["subagent_model"] = arg.split("=", 1)[1]

        elif arg == "--subagent-model":
            if i + 1 < len(argv):
                i += 1
                args["subagent_model"] = argv[i]
            else:
                print("error: --subagent-model requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg == "--emit-board":
            args["emit_board"] = True

        elif arg == "--emit-raw":
            args["emit_raw"] = True

        elif arg == "--emit-subagent-raw":
            args["emit_subagent_raw"] = True

        elif arg.startswith("--context-mode="):
            mode = arg.split("=", 1)[1]
            if mode not in ("fresh", "accumulate"):
                print("error: --context-mode must be 'fresh' or 'accumulate'", file=sys.stderr)
                sys.exit(1)
            args["context_mode"] = mode

        elif arg == "--context-mode":
            if i + 1 < len(argv):
                i += 1
                mode = argv[i]
                if mode not in ("fresh", "accumulate"):
                    print("error: --context-mode must be 'fresh' or 'accumulate'", file=sys.stderr)
                    sys.exit(1)
                args["context_mode"] = mode
            else:
                print("error: --context-mode requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg == "--stdin-input":
            args["stdin_input"] = True
            # Reserved for future web viewer integration

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
Usage: python -m cli run <session-path> [options]

Run an A7-RT session headlessly.

Arguments:
  session-path             Path to A7-RT session (required)

Options:
  --stage-id=<id>          Stage to run (required if multiple stages)
  --turn-limit=<N>         Maximum turns (default: 200, 0=unlimited)
  --context-mode=<mode>    Subagent context: 'fresh' (reset each turn) or 'accumulate' (default: fresh)
  --daemon                 Run until complete/halted (unlimited turns)
  --model=<name>           Manager model override
  --subagent-model=<name>  Subagent model override
  --emit-board             Emit manager board to stderr (JSONL)
  --emit-raw               Emit raw LLM responses to stderr (JSONL)
  --emit-subagent-raw      Emit subagent responses to stderr (JSONL)
  --no-consult             Disable A7 CONSULT oracle
  --help, -h               Show this help message

Exit Codes:
  0 — Session sealed (success)
  1 — HALT condition (needs human review)
  2 — Crash (unexpected error)
  3 — Turn limit reached

Examples:
  python -m cli run /tmp/my-session --stage-id=stage-1
  python -m cli run /tmp/my-session --turn-limit=20 --emit-board 2>telemetry.jsonl
  python -m cli run /tmp/my-session --daemon --no-consult
"""
