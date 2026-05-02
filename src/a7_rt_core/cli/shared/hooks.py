# cli/shared/hooks.py — Manager hook utilities for CLI

"""Shared manager hook factory for CLI commands.

This module provides utilities for creating manager hooks that handle
human input requests and state updates during headless execution.

Typical usage:
    from cli.shared.hooks import create_manager_hook

    hook = create_manager_hook(
        session_path="/path/to/session",
        handle_halt=True,  # or False for fully automated
    )
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

ManagerHook = Callable[[dict[str, Any]], dict[str, Any] | None]


def create_manager_hook(
    session_path: Path,
    *,
    handle_halt: bool = False,
    stdin_input: bool = False,
    print_callback: Callable[[str], None] | None = None,
) -> ManagerHook:
    """Create a manager hook for CLI execution.

    Args:
        session_path: Path to the A7-RT session
        handle_halt: Whether to prompt for input on HALT conditions
        stdin_input: Whether to read human input from stdin
        print_callback: Optional callback for status messages

    Returns:
        A manager hook function suitable for A7Engine
    """

    def _print(msg: str) -> None:
        if print_callback:
            print_callback(msg)
        else:
            print(msg, file=sys.stderr)

    def manager_hook(event: dict[str, Any]) -> dict[str, Any] | None:
        """Process manager events and handle human input requests.

        Args:
            event: Event dict with 'type' and other fields

        Returns:
            Response dict for input requests, or None
        """
        event_type = event.get("type")

        if event_type == "human_input_requested":
            _print("=" * 60)
            _print("HUMAN INPUT REQUESTED")
            _print("=" * 60)

            question = event.get("question", "Input required:")
            _print(f"\nQuestion: {question}")

            if not handle_halt:
                _print("\n(Automated mode - skipping human input)")
                return {
                    "type": "human_input",
                    "response": "AUTOMATED_SKIP",
                    "timestamp": _now_iso(),
                }

            if stdin_input:
                _print("\nEnter response (Ctrl+D to finish):")
                lines = []
                try:
                    while True:
                        line = input()
                        lines.append(line)
                except EOFError:
                    pass
                response = "\n".join(lines).strip()
            else:
                # Simple single-line input
                try:
                    response = input("\nResponse: ")
                except EOFError:
                    response = ""

            return {
                "type": "human_input",
                "response": response,
                "timestamp": _now_iso(),
            }

        elif event_type == "state_updated":
            # State update notification - log if verbose
            turn = event.get("turn", 0)
            status = event.get("status", "unknown")
            _print(f"  [Turn {turn}] Status: {status}")

        elif event_type == "halt":
            _print("!" * 60)
            _print("HALT CONDITION")
            _print("!" * 60)
            _print(f"Reason: {event.get('reason', 'unknown')}")
            _print(f"Node: {event.get('node_id', 'N/A')}")

        elif event_type == "seal_review":
            _print("=" * 60)
            _print("STAGE SEAL REVIEW")
            _print("=" * 60)
            _print(f"Stage: {event.get('stage_id', 'unknown')}")
            grounded = event.get("grounded_count", 0)
            total = event.get("total_count", 0)
            _print(f"Nodes grounded: {grounded}/{total}")

        return None

    return manager_hook


def _now_iso() -> str:
    """Return current timestamp in ISO format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def create_silent_hook() -> ManagerHook:
    """Create a no-op manager hook that ignores all events.

    Use this for fully automated execution with no human intervention.
    """
    return lambda event: None


def create_logging_hook(
    log_path: Path,
    *,
    echo: bool = True,
) -> ManagerHook:
    """Create a manager hook that logs all events to a file.

    Args:
        log_path: Path to log file (append mode)
        echo: Whether to also print to stderr

    Returns:
        A manager hook that logs events
    """
    import json

    def hook(event: dict[str, Any]) -> None:
        # Add timestamp
        event_with_time = {
            **event,
            "logged_at": _now_iso(),
        }

        # Append to log file
        with open(log_path, "a") as f:
            f.write(json.dumps(event_with_time) + "\n")

        # Echo if requested
        if echo:
            print(f"[HOOK] {event.get('type', 'unknown')}", file=sys.stderr)

    return hook


def compose_hooks(*hooks: ManagerHook) -> ManagerHook:
    """Compose multiple hooks into a single hook.

    Each hook is called in order. The first non-None return value
    is used as the response.
    """

    def composed(event: dict[str, Any]) -> dict[str, Any] | None:
        for hook_fn in hooks:
            result = hook_fn(event)
            if result is not None:
                return result
        return None

    return composed
