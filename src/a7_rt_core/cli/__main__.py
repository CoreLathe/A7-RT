# a7-rt-core/src/a7_rt_core/cli/__main__.py — A7-RT CLI entry point (package version)

"""A7-RT Command Line Interface (package version).

Usage:
    python -m a7_rt_core.cli <command> [options]

Commands:
    init <session-path>      Initialize a new A7-RT session
    seed <session-path>      Add nodes to an existing session
    replace <session-path>   Replace a node with a new implementation
    stage-reset <session-path> <stage-id>  Reset a stage to start fresh
    run <session-path>       Run session headlessly
    config                   Manage configuration
    stage <session-path>     Create a new stage
    narrative <session-path> View session narrative
    web                      Start web interface (stub)

Examples:
    python -m a7_rt_core.cli init /tmp/my-session --source=/path/to/code
    python -m a7_rt_core.cli run /tmp/my-session --stage-id=stage-1
    python -m a7_rt_core.cli config --models
"""

from __future__ import annotations

import sys
from typing import NoReturn


def _show_help() -> None:
    """Display help message."""
    help_text = """
A7-RT Command Line Interface (package version)

Usage:
    python -m a7_rt_core.cli <command> [options]

Commands:
    init <session-path>      Initialize a new A7-RT session
    seed <session-path>      Add nodes to an existing session
    replace <session-path>   Replace a node with a new implementation
    stage-reset <session-path> <stage-id>  Reset a stage to start fresh
    run <session-path>       Run session headlessly
    config                   Manage configuration
    stage <session-path>     Create a new stage
    narrative <session-path> View session narrative
    web                      Start web interface (stub)
    help                     Show this help message

Global Options:
    --help, -h               Show help for a command

Examples:
    # Initialize a new session
    python -m a7_rt_core.cli init /tmp/my-session

    # Run a session headlessly
    python -m a7_rt_core.cli run /tmp/my-session --stage-id=stage-1

    # View available models
    python -m a7_rt_core.cli config --models

For command-specific help:
    python -m a7_rt_core.cli <command> --help
"""
    print(help_text)


def main() -> int:
    """Main entry point."""
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h", "help"):
        _show_help()
        return 0

    command = args[0]
    remaining = args[1:]

    # Route to appropriate command handler
    if command == "init":
        from a7_rt_core.cli.commands.init import run_init

        return run_init(remaining)

    elif command == "seed":
        from a7_rt_core.cli.commands.seed import run_seed

        return run_seed(remaining)

    elif command == "replace":
        from a7_rt_core.cli.commands.replace import run_replace

        return run_replace(remaining)

    elif command == "run":
        from a7_rt_core.cli.commands.run import run_headless

        return run_headless(remaining)

    elif command == "config":
        from a7_rt_core.cli.commands.config import run_config

        return run_config(remaining)

    elif command == "stage":
        from a7_rt_core.cli.commands.stage import run_stage_create

        return run_stage_create(remaining)

    elif command == "stage-reset":
        from a7_rt_core.cli.commands.stage_reset import run_stage_reset

        return run_stage_reset(remaining)

    elif command == "narrative":
        from a7_rt_core.cli.commands.narrative import run_narrative

        return run_narrative(remaining)

    elif command == "web":
        from a7_rt_core.cli.web.stub import run_web_server

        return run_web_server()

    else:
        print(f"error: unknown command: {command!r}", file=sys.stderr)
        print("Run 'python -m a7_rt_core.cli help' for usage.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
