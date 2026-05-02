# a7-rt-core/src/a7_rt_core/cli/runner.py — Headless runner for A7-RT (package version)
#
# Bounded, observable, resumable execution path for CI/CD and automation.
# No TUI dependency. Pure harness loop with JSONL stdout.
#
# Usage:
#   python -m cli run <session-path> --stage-id=<id> [options]
#
# JSONL events are emitted to stdout after every turn boundary.
# Progress messages and errors go to stderr.
#
# Exit codes:
#   0 — sealed (complete success, lifecycle ended with SEAL)
#   1 — halted (HaltSignal raised — needs human review)
#   2 — crashed (unexpected exception)
#   3 — turn limit reached

from __future__ import annotations

import json
import os
import sys
import traceback as tb
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Path bootstrap — for running without pip install
# ---------------------------------------------------------------------------

# This file lives at src/a7_rt_core/cli/runner.py.
# src/ must be on sys.path for the a7_rt_core package to be importable.
cli_dir = Path(__file__).resolve().parent
src_dir = cli_dir.parent.parent.parent  # src/a7_rt_core/cli/ -> src/
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


# ---------------------------------------------------------------------------
# Imports (after path bootstrap)
# ---------------------------------------------------------------------------

from a7_rt_core.core.config import (
    DEFAULT_MANAGER_MODEL,
    DEFAULT_SUBAGENT_MODEL,
    A7Config,
    load_config,
)
from a7_rt_core.data import get_protocols_dir, get_roles_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RUNNER_TURN_LIMIT: int = 200

# Exit codes — documented in module docstring above
EXIT_SEALED: int = 0
EXIT_HALTED: int = 1
EXIT_CRASHED: int = 2
EXIT_TURN_LIMIT: int = 3

# Board emission target — stderr so stdout stays pure JSONL
_BOARD_STREAM = sys.stderr


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

_HELP = """\
Usage:
  python -m tui run <session-path> --stage-id=<id> [options]

  Headless execution path for CI/CD and automation.
  No TUI — pure harness loop with JSONL events emitted to stdout.

━━━  Arguments  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  session-path           Path to an existing A7-RT session root.
                         Must contain master.json. Run 'init' first if needed.

  --stage-id=<id>        (required) Stage to run or resume.

━━━  Options  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --turn-limit=N         Hard turn limit before exit code 3.
                         Default: 200. Set to 0 to disable (run until dead).

  --model=<name>         Manager LLM model.
                         Default: anthropic/claude-sonnet-4-6

  --subagent-model=<name>
                         Subagent LLM model for code-generation work.
                         Default: anthropic/claude-haiku-4.5

  --resume               Resume an existing session (default behaviour when
                         manager.json is present; flag is informational).

  --no-consult           Disable the A7 CONSULT hook (pass None to harness).

  --stdin-input          Read JSON lines from stdin and inject them into the
                         harness as human input events at each turn boundary.
                         Each line must be a JSON object. The "detail" field
                         drives routing (slash commands: /halt, /suspend,
                         /verify; plain text: appended to human_input_queue).
                         EOF or an empty line closes the input stream cleanly.

                         Examples:
                           echo '{"detail":"/halt too many retries"}' | ...
                           echo '{"detail":"focus on auth module first"}' | ...
                           echo '{"detail":"/suspend auth.jwt near"}' | ...

  --emit-board           Emit the manager board view to stderr as JSONL
                         before each manager LLM call. Each line is:
                           {"turn": N, "event": "board", "board": {...}}
                         Pipe stderr separately to capture:
                           python -m tui run ... --emit-board 2>boards.jsonl

  --emit-raw             Emit the raw manager LLM response to stderr as JSONL
                         after each LLM call (before parsing). Each line is:
                           {"turn": N, "event": "raw_response", "response": "..."}
                         Useful for debugging why the manager parser failed.

  --emit-subagent-raw    Emit raw subagent (builder/test_author/analyst) responses
                         to stderr as JSONL. Each line is:
                           {"turn": N, "event": "subagent_raw", "node_id": "...", "role": "...", "data": {...}}
                         Useful for debugging subagent behavior and file submissions.

  -h, --help             Show this message and exit.

━━━  JSONL output  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  stdout — one JSON object per line (always):

    {"turn": 0, "event": "session_start", "stage_id": "s1", ...}
    {"turn": 1, "event": "step", "mode": "autonomous", ...}
    {"turn": 2, "event": "halt", "reason": "..."}
    {"turn": 2, "event": "sealed"}
    {"turn": 2, "event": "crashed", "error": "...", "traceback": "..."}
    {"turn": 2, "event": "turn_limit", "turns_used": 100}

  stderr — board view per turn (only with --emit-board):

    {"turn": 1, "event": "board", "board": {"nodes": {...}, "in_flight": [], ...}}

━━━  Exit codes  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  0   sealed     — stage sealed successfully
  1   halted     — HaltSignal raised (needs human review)
  2   crashed    — unexpected exception
  3   turn_limit — hard turn limit reached before death

━━━  Examples  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # Run a stage to completion:
  python -m tui run /tmp/my-session --stage-id=stage-1

  # Resume a session with a lower model tier:
  python -m tui run /tmp/my-session --stage-id=stage-1 --resume \\
      --model=anthropic/claude-haiku-4.5

  # CI mode — stream JSONL, cap at 50 turns, disable CONSULT:
  python -m tui run /tmp/my-session --stage-id=stage-1 \\
      --turn-limit=50 --no-consult | tee run.jsonl

  # Check last event for the exit condition:
  tail -1 run.jsonl | python -m json.tool

  # Inject human guidance via stdin:
  echo '{"detail":"prioritise the auth module"}' | \\
      python -m tui run /tmp/my-session --stage-id=stage-1 --stdin-input

  # Capture board views separately from events:
  python -m tui run /tmp/my-session --stage-id=stage-1 \\
      --emit-board 2>boards.jsonl | tee events.jsonl

  # Both — full observability:
  echo '{"detail":"/halt stop now"}' | \\
      python -m tui run /tmp/my-session --stage-id=stage-1 \\
      --stdin-input --emit-board 2>boards.jsonl | tee events.jsonl
"""


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class _RunArgs:
    """Parsed arguments for the `run` subcommand."""

    def __init__(self) -> None:
        self.session_path: str | None = None
        self.stage_id: str | None = None
        self.runner_turn_limit: int = _DEFAULT_RUNNER_TURN_LIMIT
        self.context_mode: str = "accumulate"  # 'fresh' (experimental) or 'accumulate' (default)
        self.model: str | None = None
        self.subagent_model: str | None = None
        self.resume: bool = False
        self.no_consult: bool = False
        self.stdin_input: bool = False
        self.emit_board: bool = False
        self.emit_raw: bool = False
        self.emit_subagent_raw: bool = False
        self.help_requested: bool = False


def _parse_run_args(argv: list[str]) -> _RunArgs:
    """
    Parse argv for the `run` subcommand. No argparse — manual parsing only,
    consistent with the rest of the codebase.

    argv is already stripped of the 'run' token by __main__.py, so argv[0]
    is either the session path or the first flag.

    Recognised forms:
        <session-path>                   positional
        --stage-id=<id>                  required option
        --stage-id <id>                  space-separated form
        --turn-limit=N / --turn-limit N
        --model=<name> / --model <name>
        --subagent-model=<name>
        --resume                         flag
        --no-consult                     flag
        -h / --help                      flag
    """
    result = _RunArgs()

    if not argv or "-h" in argv or "--help" in argv:
        result.help_requested = True
        return result

    i = 0
    while i < len(argv):
        arg = argv[i]

        # ── key=value helpers ────────────────────────────────────────────────
        def _kv(prefix: str) -> str:
            """Extract value from --key=value or --key <value> form."""
            nonlocal i
            if "=" in arg:
                return arg.split("=", 1)[1]
            if i + 1 < len(argv):
                i += 1
                return argv[i]
            print(f"error: {prefix} requires a value", file=sys.stderr)
            sys.exit(1)

        # ── option dispatch ──────────────────────────────────────────────────
        if arg.startswith("--stage-id"):
            result.stage_id = _kv("--stage-id")
        elif arg.startswith("--turn-limit"):
            raw = _kv("--turn-limit")
            try:
                result.runner_turn_limit = int(raw)
            except ValueError:
                print(
                    f"error: --turn-limit must be a non-negative integer, got {raw!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if result.runner_turn_limit < 0:
                print(
                    f"error: --turn-limit must be >= 0, got {result.runner_turn_limit}",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif arg.startswith("--context-mode"):
            mode = _kv("--context-mode")
            if mode not in ("fresh", "accumulate"):
                print(
                    f"error: --context-mode must be 'fresh' or 'accumulate', got {mode!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            result.context_mode = mode
        elif arg.startswith("--subagent-model"):
            result.subagent_model = _kv("--subagent-model")
        elif arg.startswith("--model"):
            result.model = _kv("--model")
        elif arg == "--resume":
            result.resume = True
        elif arg == "--no-consult":
            result.no_consult = True
        elif arg == "--stdin-input":
            result.stdin_input = True
        elif arg == "--emit-board":
            result.emit_board = True
        elif arg == "--emit-raw":
            result.emit_raw = True
        elif arg == "--emit-subagent-raw":
            result.emit_subagent_raw = True
        elif not arg.startswith("-"):
            # Positional — the session path
            if result.session_path is None:
                result.session_path = arg
            else:
                print(
                    f"error: unexpected positional argument: {arg!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            print(
                f"error: unknown option: {arg!r}\n       python -m tui run --help",
                file=sys.stderr,
            )
            sys.exit(1)

        i += 1

    return result


# ---------------------------------------------------------------------------
# Session path validation
# ---------------------------------------------------------------------------


def _validate_session_path(session_path: str) -> Path:
    """
    Validate the session path and return it as a resolved Path.

    Prints a clear error and exits on any problem:
      - Path does not exist
      - Path is not a directory
      - master.json is missing
    """
    p = Path(session_path).expanduser().resolve()

    if not p.exists():
        print(
            f"error: session path does not exist: {p}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not p.is_dir():
        print(
            f"error: session path is not a directory: {p}",
            file=sys.stderr,
        )
        sys.exit(1)

    master = p / "master.json"
    if not master.exists():
        print(
            f"error: no master.json found at {p}\n"
            "       This directory does not appear to be an A7-RT session root.\n"
            f"       Run: python -m tui init {p}",
            file=sys.stderr,
        )
        sys.exit(1)

    return p


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------


def _load_api_key(config: Optional[A7Config] = None) -> str:
    """
    Resolve API key using the config system.

    Preference order:
      1. Configured provider's api_key_env environment variable
      2. Session-local .a7/key file
      3. Project-level .a7/key file
      4. User-level ~/.a7/key file
      5. Legacy OPENROUTER_API_KEY env var (fallback)

    Returns the key string, or "" if not found anywhere.
    """
    # Use config system if available - try all configured providers
    if config and config.providers:
        for provider_name in config.providers:
            key = config.resolve_api_key(provider_name)
            if key:
                return key

    # Fallback to legacy env var
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key

    return ""


# ---------------------------------------------------------------------------
# Manager hook factory
# ---------------------------------------------------------------------------


def _make_manager_hook(
    llm: Any,
    roles_dir: Path,
    emit_board: bool = False,
    emit_raw: bool = False,
    model_config: Any = None,
) -> Callable:
    """
    Build a callable manager hook from an LLMClient and the roles directory.

    The hook signature matches ManagerHook:
        (board: dict, state: ManagerState) → ManagerAction

    Reads manager_prompt.md as the system prompt, sends the board JSON as the
    user message, and delegates response parsing to manager_parser. On any LLM
    error it returns HaltAction so the harness can checkpoint cleanly.

    If emit_board is True, the board dict is emitted to stderr as JSONL before
    each LLM call:
        {"turn": N, "event": "board", "board": {...}}

    If emit_raw is True, the raw LLM response is emitted to stderr after each
    LLM call (before parsing):
        {"turn": N, "event": "raw_response", "response": "..."}

    This function is a standalone replication of A7App._make_manager_hook
    from tui/app.py, without any Textual dependency (no self.log, etc.).

    Phase 6+ migration: Uses native function calling with submit_action tool.
    Legacy ACT: JSON parsing remains available as fallback.
    """
    from a7_rt_core.core.models import HaltAction
    from a7_rt_core.llm.parser import (
        MANAGER_TOOL_CHOICE,
        MANAGER_TOOLS,
        handle_manager_response,
    )

    prompt_path = roles_dir / "manager_prompt.md"
    manager_prompt: str = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    if not manager_prompt:
        print(
            f"warning: {prompt_path} not found — manager running without system prompt",
            file=sys.stderr,
        )

    def manager_hook(board: dict, state: object) -> object:
        # Emit board to stderr before the LLM call if requested
        if emit_board:
            turn = getattr(state, "turn", 0)
            print(
                json.dumps(
                    {"turn": turn, "event": "board", "board": board},
                    default=str,
                ),
                file=_BOARD_STREAM,
                flush=True,
            )

        board_json = json.dumps(board, indent=2, default=str)
        messages = [
            {"role": "system", "content": manager_prompt},
            {"role": "user", "content": board_json},
        ]

        # Manager uses native function calling with forced tool choice
        # Auto-inject reasoning disable for compatibility with thinking models
        try:
            kwargs: dict[str, Any] = dict(
                messages=messages,
                max_tokens=4096,
                tools=MANAGER_TOOLS,
                tool_choice=MANAGER_TOOL_CHOICE,
            )
            # Auto-disable thinking for manager (state machine needs reactive, not deliberative)
            extra_body: dict[str, Any] = {"reasoning": {"effort": "none"}}
            if model_config and hasattr(model_config, "extra_body") and model_config.extra_body:
                extra_body.update(model_config.extra_body)
            kwargs["extra_body"] = extra_body
            response = llm.call_with_messages(**kwargs)
        except Exception as exc:
            return HaltAction(reason=f"manager LLM error: {type(exc).__name__}: {exc}")

        # Emit raw response to stderr if requested (for debugging)
        if emit_raw:
            turn = getattr(state, "turn", 0)
            raw_display = response.content
            if response.tool_calls:
                raw_display += f"\n[tool_calls: {response.tool_calls}]"
            print(
                json.dumps(
                    {"turn": turn, "event": "raw_response", "response": raw_display},
                    default=str,
                ),
                file=_BOARD_STREAM,
                flush=True,
            )

        # Handle response: native function calling only
        if response.tool_calls:
            return handle_manager_response(response)
        else:
            # No tool calls received - this shouldn't happen with forced tool_choice
            # but handle gracefully
            from a7_rt_core.llm.parser import parse_manager_action

            response_text = response.content or ""
            if not response_text:
                return HaltAction(
                    reason="manager returned empty response (no tool calls, no content)"
                )

            # Emit raw response if requested
            if emit_raw:
                turn = getattr(state, "turn", 0)
                print(
                    json.dumps(
                        {"turn": turn, "event": "raw_response", "response": response_text},
                        default=str,
                    ),
                    file=_BOARD_STREAM,
                    flush=True,
                )

            return parse_manager_action(response_text)

    return manager_hook


# ---------------------------------------------------------------------------
# Stdin injection thread
# ---------------------------------------------------------------------------


def _start_stdin_reader(harness: Any) -> None:
    """
    Start a daemon thread that reads JSON lines from stdin and injects each
    one into the harness via enqueue_human_input().

    The thread is a daemon so it never blocks process exit. It terminates
    naturally on EOF (pipe closed by the producer) or on an empty line.

    Each stdin line must be a JSON object. The harness routes on the "detail"
    field at the next turn boundary:

        /halt [reason]           → checkpoint + raise HaltSignal
        /suspend <node> <type>   → suspend node in master.json
        /verify <from> <to>      → mark assumption edge verified
        plain text               → appended to human_input_queue on board

    Malformed JSON lines are silently dropped (logged to stderr).

    Threading contract:
        enqueue_human_input() is documented as thread-safe (queue.Queue).
        This thread never touches master.json or harness state directly.
    """
    import threading

    def _reader() -> None:
        try:
            for raw_line in sys.stdin:
                line = raw_line.strip()
                if not line:
                    # Empty line signals clean EOF from producer
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"warning: stdin injection — malformed JSON dropped: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if not isinstance(event, dict):
                    print(
                        "warning: stdin injection — line is not a JSON object, dropped",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                harness.enqueue_human_input(event)
        except Exception:
            # Never crash the reader thread — harness continues without injection
            pass

    t = threading.Thread(target=_reader, name="StdinInjector", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# JSONL emitter
# ---------------------------------------------------------------------------


def _emit(obj: dict[str, Any]) -> None:
    """
    Emit a single JSONL record to stdout, flushing immediately.

    All JSONL output goes to stdout. Progress and errors go to stderr.
    Flushing after every line ensures CI/CD consumers see events in real time.
    """
    print(json.dumps(obj, default=str), flush=True)


# ---------------------------------------------------------------------------
# Last harness event extraction
# ---------------------------------------------------------------------------


def _last_harness_action(repo: Any, min_turn: int) -> tuple[str | None, str | None]:
    """
    Scan events.jsonl for the most recent event where actor == 'harness'
    at or after min_turn, and extract (action, target).

    Returns (action, target) with either field possibly None if not found
    or the events log is empty.

    We search from the end to get the most recent matching event efficiently.
    """
    try:
        events = repo.get_events(since_turn=min_turn)
    except Exception:
        return None, None

    # Traverse in reverse to find the most recent harness action
    for event in reversed(events):
        if event.get("actor") == "harness":
            return (
                event.get("action"),
                event.get("target"),
            )
    return None, None


# ---------------------------------------------------------------------------
# Stage sealed detection
# ---------------------------------------------------------------------------


def _stage_is_sealed(repo: Any, stage_id: str) -> bool:
    """
    Return True if the stage has status == 'sealed' in master.json.

    Used to confirm a SEAL exit path after the harness returns is_dead.
    Falls back to False on any read/parse error.
    """
    try:
        doc = repo._load()
        stage = doc.get("stages", {}).get(stage_id, {})
        return stage.get("status") == "sealed"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main harness loop
# ---------------------------------------------------------------------------


def _run_loop(
    *,
    harness: Any,
    stage_id: str,
    args: _RunArgs,
) -> int:
    """
    Execute the init_session() / step() loop and emit JSONL to stdout.

    Returns one of the EXIT_* integer constants (0–3) that the caller
    should pass to sys.exit().

    Does not call sys.exit() directly so that inline tests can inspect the
    return value without the process terminating.

    If args.stdin_input is True, a daemon thread is started before the loop
    that reads JSON lines from stdin and injects them via enqueue_human_input().
    The thread is daemon — it never blocks process exit.

    If args.emit_board is True, the manager board is emitted to stderr as
    JSONL before each LLM call (handled inside the manager hook wrapper).
    """
    from a7_rt_core.harness.core import (
        HaltSignal,  # local import — avoids module-level circular dep
    )

    # Start stdin injection thread if requested (before init_session so any
    # early human messages are queued before the first turn boundary).
    if args.stdin_input:
        _start_stdin_reader(harness)

    state = None  # last known state; updated after every successful call
    turn_used: int = 0

    try:
        # ── Initialise session ───────────────────────────────────────────────
        state = harness.init_session(stage_id)
        turn_used = state.turn

        _emit(
            {
                "turn": state.turn,
                "event": "session_start",
                "stage_id": stage_id,
                "model": args.model,
                "subagent_model": args.subagent_model,
                "manager_max_turns": state.manager_max_turns,
                "runner_turn_limit": args.runner_turn_limit,
                "lifecycle": harness._lifecycle,
            }
        )

        # ── Step loop ────────────────────────────────────────────────────────
        while not state.is_dead:
            # Hard turn limit (0 means unlimited)
            if args.runner_turn_limit > 0 and state.turn >= args.runner_turn_limit:
                _emit(
                    {
                        "turn": state.turn,
                        "event": "turn_limit",
                        "turns_used": state.turn,
                    }
                )
                return EXIT_TURN_LIMIT

            prev_turn = state.turn
            state = harness.step(state)
            turn_used = state.turn

            # Extract the action the harness just executed from the event log
            action_name, action_target = _last_harness_action(harness.repo, min_turn=prev_turn)

            _emit(
                {
                    "turn": state.turn,
                    "event": "step",
                    "mode": state.mode.value,
                    "in_flight": list(state.in_flight),
                    "action": action_name,
                    "node": action_target,
                }
            )

        # ── Dead — determine why ─────────────────────────────────────────────
        if _stage_is_sealed(harness.repo, stage_id):
            _emit({"turn": state.turn, "event": "sealed"})
            return EXIT_SEALED

        # Dead but not sealed — turn limit enforced internally by harness
        _emit(
            {
                "turn": state.turn,
                "event": "turn_limit",
                "turns_used": state.turn,
            }
        )
        return EXIT_TURN_LIMIT

    except HaltSignal as exc:
        current_turn = state.turn if state is not None else turn_used
        _emit(
            {
                "turn": current_turn,
                "event": "halt",
                "reason": exc.reason,
            }
        )
        return EXIT_HALTED

    except Exception as exc:  # noqa: BLE001
        current_turn = state.turn if state is not None else turn_used
        _emit(
            {
                "turn": current_turn,
                "event": "crashed",
                "error": str(exc),
                "traceback": "".join(tb.format_exception(type(exc), exc, exc.__traceback__)),
            }
        )
        return EXIT_CRASHED


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_headless(argv: list[str]) -> None:
    """
    Entry point called from __main__.py when subcommand is 'run'.

    Parses argv (already stripped of the 'run' token), validates the session,
    wires the harness, runs the loop, and exits with the appropriate code.

    Parameters
    ----------
    argv : list[str]
        Argument list with 'run' already removed (i.e. sys.argv[2:] from the
        top-level dispatcher). argv[0] is expected to be the session path or
        the first flag.
    """
    # ── Parse arguments ──────────────────────────────────────────────────────
    args = _parse_run_args(argv)

    if args.help_requested:
        print(_HELP, end="")
        sys.exit(0)

    if not args.session_path:
        print(
            "error: session-path is required.\n"
            "Usage: python -m tui run <session-path> --stage-id=<id>\n"
            "       python -m tui run --help",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.stage_id:
        print(
            "error: --stage-id is required.\n"
            "Usage: python -m tui run <session-path> --stage-id=<id>\n"
            "       python -m tui run --help",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Validate session path ────────────────────────────────────────────────
    session_path = _validate_session_path(args.session_path)

    # ── Load configuration from .a7 hierarchy ────────────────────────────────
    cli_overrides: dict[str, Any] = {}
    if args.model is not None:
        cli_overrides["manager_model"] = args.model
    if args.subagent_model is not None:
        cli_overrides["subagent_model"] = args.subagent_model

    config = load_config(
        session_path=session_path,
        cli_overrides=cli_overrides if cli_overrides else None,
    )

    # Resolve effective models (handles aliases from config)
    manager_model_id = config.get_effective_model("manager")
    subagent_model_id = config.get_effective_model("subagent")

    # ── Announce to stderr ───────────────────────────────────────────────────
    print(
        f"A7-RT headless runner — session={session_path} "
        f"stage-id={args.stage_id!r} "
        f"turns={args.runner_turn_limit if args.runner_turn_limit > 0 else 'unlimited'} "
        f"manager={manager_model_id} subagent={subagent_model_id}",
        file=sys.stderr,
    )

    # ── Load API key ─────────────────────────────────────────────────────────
    api_key = _load_api_key(config=config)
    if not api_key:
        print(
            "error: no API key found.\n"
            "       Set your provider's API key environment variable (e.g., OPENROUTER_API_KEY), or\n"
            f"       create {session_path / '.a7' / 'key'} for session-local key.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Import core modules (deferred to avoid load-time side-effects) ────────
    try:
        from openai import OpenAI  # type: ignore[import]

        from a7_rt_core.harness.core import Harness
        from a7_rt_core.llm.a7_engine import A7Engine
        from a7_rt_core.llm.client import LLMClient
        from a7_rt_core.llm.subagent import Subagent
        from a7_rt_core.storage.repository import Repository
    except ImportError as exc:
        print(f"error: import failed: {exc}", file=sys.stderr)
        sys.exit(2)

    # ── Wire harness components ──────────────────────────────────────────────
    try:
        roles_dir = get_roles_dir()
        protocols_dir = get_protocols_dir()

        # Determine base URL from configured provider
        # Use manager model's provider to determine base_url
        base_url = "https://openrouter.ai/api/v1"  # fallback
        manager_model_config = config.get_model_config(config.manager_model)
        if manager_model_config:
            provider_config = config.get_provider_config(manager_model_config.provider)
            if provider_config and provider_config.base_url:
                base_url = provider_config.base_url

        openai_client = OpenAI(
            base_url=base_url,
            api_key=api_key,
        )

        repo = Repository.open(session_path)

        # Manager: high-capability model for board reading + action selection
        manager_llm = LLMClient(openai_client, model=manager_model_id)
        manager_hook = _make_manager_hook(
            manager_llm,
            roles_dir,
            emit_board=args.emit_board,
            emit_raw=args.emit_raw,
            model_config=manager_model_config,
        )

        # Subagent raw response callback (for debugging)
        def _subagent_raw_callback(node_id: str, role: str, data: dict) -> None:
            if args.emit_subagent_raw:
                print(
                    json.dumps(
                        {
                            "event": "subagent_raw",
                            "node_id": node_id,
                            "role": role,
                            "data": data,
                        },
                        default=str,
                    ),
                    file=_BOARD_STREAM,
                    flush=True,
                )

        # Subagent: economy model for code-generation work
        subagent_llm = LLMClient(openai_client, model=subagent_model_id)
        subagent = Subagent(
            llm=subagent_llm,
            roles_dir=roles_dir,
            protocols_dir=protocols_dir,
            content_dir=session_path / "content",
            context_mode=args.context_mode,
            on_raw_response=_subagent_raw_callback,
        )
        subagent_hook = subagent.as_hook()

        # A7 CONSULT hook (optional)
        consult_hook: Optional[Callable] = None
        if not args.no_consult:
            a7_llm = LLMClient(openai_client, model=manager_model_id)
            a7 = A7Engine(llm=a7_llm, protocols_dir=protocols_dir)
            consult_hook = a7.as_hook()

        harness = Harness(
            repo=repo,
            manager_hook=manager_hook,
            subagent_hook=subagent_hook,
            consult_hook=consult_hook,
        )

        # Apply rate limiting delay from provider config
        # Use manager model's provider for delay settings
        if manager_model_config:
            provider_config = config.get_provider_config(manager_model_config.provider)
            if provider_config and provider_config.inter_call_delay_ms > 0:
                harness.set_inter_call_delay(provider_config.inter_call_delay_ms)

    except Exception as exc:
        print(
            f"error: failed to initialise harness: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)

    # ── Run ──────────────────────────────────────────────────────────────────
    exit_code = _run_loop(
        harness=harness,
        stage_id=args.stage_id,
        args=args,
    )
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run self-contained tests without network calls.
    # Mock everything that touches the filesystem or API.

    import io
    from unittest.mock import MagicMock, patch

    # ── Additional tests for new flags ──────────────────────────────────────────

    def _test_new_flags() -> None:
        """Tests for --stdin-input, --emit-board arg parsing and stdin reader."""
        import io
        import threading

        t = _T()

        # Arg parsing — --stdin-input
        a = _parse_run_args(["mysession", "--stage-id=s1", "--stdin-input"])
        t.check("parse: --stdin-input sets stdin_input=True", a.stdin_input is True)
        t.check("parse: --stdin-input leaves emit_board=False", a.emit_board is False)

        # Arg parsing — --emit-board
        a = _parse_run_args(["mysession", "--stage-id=s1", "--emit-board"])
        t.check("parse: --emit-board sets emit_board=True", a.emit_board is True)
        t.check("parse: --emit-board leaves stdin_input=False", a.stdin_input is False)

        # Arg parsing — both flags together
        a = _parse_run_args(["mysession", "--stage-id=s1", "--stdin-input", "--emit-board"])
        t.check("parse: both flags together — stdin_input=True", a.stdin_input is True)
        t.check("parse: both flags together — emit_board=True", a.emit_board is True)

        # Arg parsing — --emit-raw flag
        a = _parse_run_args(["mysession", "--stage-id=s1", "--emit-raw"])
        t.check("parse: --emit-raw sets emit_raw=True", a.emit_raw is True)

        # Default values
        a = _parse_run_args(["mysession", "--stage-id=s1"])
        t.check("parse: stdin_input default False", a.stdin_input is False)
        t.check("parse: emit_board default False", a.emit_board is False)
        t.check("parse: emit_subagent_raw default False", a.emit_subagent_raw is False)

        # Arg parsing --emit-subagent-raw
        a = _parse_run_args(["mysession", "--stage-id=s1", "--emit-subagent-raw"])
        t.check(
            "parse: --emit-subagent-raw sets emit_subagent_raw=True",
            a.emit_subagent_raw is True,
        )

        # Stdin reader — injects valid JSON into harness queue
        injected: list = []

        class _MockHarness:
            def enqueue_human_input(self, event: dict) -> None:
                injected.append(event)

        mock_harness = _MockHarness()
        original_stdin = sys.stdin
        sys.stdin = io.StringIO('{"detail": "focus on auth"}\n{"detail": "/halt stop"}\n')
        _start_stdin_reader(mock_harness)
        import time

        time.sleep(0.1)  # give daemon thread time to drain
        sys.stdin = original_stdin
        t.check("stdin reader: two events injected", len(injected) == 2)
        t.check(
            "stdin reader: first event detail correct",
            injected[0].get("detail") == "focus on auth",
        )
        t.check(
            "stdin reader: second event detail correct",
            injected[1].get("detail") == "/halt stop",
        )

        # Stdin reader — malformed JSON is dropped (no crash)
        injected2: list = []

        class _MockHarness2:
            def enqueue_human_input(self, event: dict) -> None:
                injected2.append(event)

        mock_harness2 = _MockHarness2()
        sys.stdin = io.StringIO('not json at all\n{"detail": "valid"}\n')
        _start_stdin_reader(mock_harness2)
        time.sleep(0.1)
        sys.stdin = original_stdin
        t.check("stdin reader: malformed line dropped", len(injected2) == 1)
        t.check(
            "stdin reader: valid line after malformed still injected",
            injected2[0].get("detail") == "valid",
        )

        # Stdin reader — non-object JSON dropped
        injected3: list = []

        class _MockHarness3:
            def enqueue_human_input(self, event: dict) -> None:
                injected3.append(event)

        sys.stdin = io.StringIO('"just a string"\n{"detail": "ok"}\n')
        _start_stdin_reader(_MockHarness3())
        time.sleep(0.1)
        sys.stdin = original_stdin
        t.check("stdin reader: non-object JSON dropped", len(injected3) == 1)

        # Stdin reader — empty line causes clean stop
        injected4: list = []

        class _MockHarness4:
            def enqueue_human_input(self, event: dict) -> None:
                injected4.append(event)

        sys.stdin = io.StringIO('{"detail": "before empty"}\n\n{"detail": "after empty"}\n')
        _start_stdin_reader(_MockHarness4())
        time.sleep(0.1)
        sys.stdin = original_stdin
        t.check("stdin reader: stops at empty line", len(injected4) == 1)

        # emit_board — board emitted to stderr before LLM call
        import io as _io

        captured_stderr = _io.StringIO()

        class _MockLLM:
            def call(self, system, user, max_tokens):
                return "HALT reason=test"

        class _MockRoles:
            pass

        # emit_board — board emitted to _BOARD_STREAM before LLM call.
        # We patch the name in whichever module namespace is active
        # (__main__ when run directly, tui.run_headless when imported).
        import sys as _sys

        _mod_name = __name__  # "__main__" or "tui.run_headless"
        _patch_target = f"{_mod_name}._BOARD_STREAM"

        roles_dir_mock = Path("/nonexistent_roles")

        class _MockState:
            turn = 7

        board_sample = {"nodes": {"a": {"status": "near"}}, "in_flight": []}

        from unittest.mock import patch as _patch

        captured_stderr = _io.StringIO()
        with _patch(_patch_target, captured_stderr):
            hook = _make_manager_hook(_MockLLM(), roles_dir_mock, emit_board=True)
            hook(board_sample, _MockState())

        board_output = captured_stderr.getvalue().strip()
        t.check("emit_board: stderr output non-empty", len(board_output) > 0)
        try:
            parsed_board_line = json.loads(board_output)
        except Exception:
            parsed_board_line = {}
        t.check("emit_board: event == 'board'", parsed_board_line.get("event") == "board")
        t.check("emit_board: turn == 7", parsed_board_line.get("turn") == 7)
        t.check("emit_board: board key present", "board" in parsed_board_line)
        t.check(
            "emit_board: board contains nodes",
            "nodes" in parsed_board_line.get("board", {}),
        )

        # emit_board=False — nothing emitted
        captured_stderr2 = _io.StringIO()
        with _patch(_patch_target, captured_stderr2):
            hook_no_board = _make_manager_hook(_MockLLM(), roles_dir_mock, emit_board=False)
            hook_no_board(board_sample, _MockState())
        t.check(
            "emit_board=False: nothing written to stderr",
            captured_stderr2.getvalue() == "",
        )

        t.summary()

    class _T:
        """Minimal test harness — no unittest runner needed."""

        _passed: int = 0
        _failed: int = 0

        @classmethod
        def check(cls, desc: str, cond: bool) -> None:
            if cond:
                cls._passed += 1
                print(f"  ✓  {desc}")
            else:
                cls._failed += 1
                print(f"  ✗  FAIL: {desc}")

        @classmethod
        def summary(cls) -> None:
            total = cls._passed + cls._failed
            print(f"\n{cls._passed}/{total} checks passed", end="")
            if cls._failed:
                print(f" — {cls._failed} FAILED")
                sys.exit(1)
            else:
                print(" — all good!")

    T = _T

    print("Running run_headless.py inline tests ...\n")

    # ── 1: Help flag ─────────────────────────────────────────────────────────
    args = _parse_run_args(["-h"])
    T.check("parse: -h sets help_requested", args.help_requested)

    args = _parse_run_args(["--help"])
    T.check("parse: --help sets help_requested", args.help_requested)

    args = _parse_run_args([])
    T.check("parse: empty argv sets help_requested", args.help_requested)

    # ── 2: Positional session path ────────────────────────────────────────────
    args = _parse_run_args(["/tmp/session", "--stage-id=s1"])
    T.check("parse: positional session path", args.session_path == "/tmp/session")
    T.check("parse: --stage-id=s1", args.stage_id == "s1")

    # ── 3: Space-separated --stage-id ────────────────────────────────────────
    args = _parse_run_args(["/tmp/session", "--stage-id", "alpha"])
    T.check("parse: --stage-id <value> (space form)", args.stage_id == "alpha")

    # ── 4: Defaults ───────────────────────────────────────────────────────────
    args = _parse_run_args(["/tmp/s", "--stage-id=x"])
    T.check(
        "parse: default runner_turn_limit", args.runner_turn_limit == _DEFAULT_RUNNER_TURN_LIMIT
    )
    T.check("parse: default model", args.model == DEFAULT_MANAGER_MODEL)
    T.check("parse: default subagent model", args.subagent_model == DEFAULT_SUBAGENT_MODEL)
    T.check("parse: resume default False", args.resume is False)
    T.check("parse: no_consult default False", args.no_consult is False)

    # ── 5: All flags ──────────────────────────────────────────────────────────
    args = _parse_run_args(
        [
            "/tmp/sess",
            "--stage-id=beta",
            "--turn-limit=42",
            "--model=anthropic/claude-opus-4",
            "--subagent-model=anthropic/claude-haiku-4.5",
            "--resume",
            "--no-consult",
        ]
    )
    T.check("parse: --turn-limit=42", args.runner_turn_limit == 42)
    T.check("parse: --model custom", args.model == "anthropic/claude-opus-4")
    T.check(
        "parse: --subagent-model custom",
        args.subagent_model == "anthropic/claude-haiku-4.5",
    )
    T.check("parse: --resume flag", args.resume is True)
    T.check("parse: --no-consult flag", args.no_consult is True)

    # ── 6: --turn-limit space-separated ────────────────────────────────────────────
    args = _parse_run_args(["/tmp/s", "--stage-id=x", "--turn-limit", "77"])
    T.check("parse: --turn-limit <N> (space form)", args.runner_turn_limit == 77)

    # ── 7: --turn-limit=0 (unlimited) ──────────────────────────────────────────────
    args = _parse_run_args(["/tmp/s", "--stage-id=x", "--turn-limit=0"])
    T.check("parse: --turn-limit=0 means unlimited", args.runner_turn_limit == 0)

    # ── 8: Missing required stage-id flagged as None ─────────────────────────
    args = _parse_run_args(["/tmp/sess"])
    T.check("parse: missing --stage-id leaves stage_id=None", args.stage_id is None)
    T.check("parse: help_requested stays False when path given", not args.help_requested)

    # ── 9: API key — env var ──────────────────────────────────────────────────
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test-env"}):
        key = _load_api_key(Path("/nonexistent"))
        T.check("api_key: reads OPENROUTER_API_KEY from env", key == "sk-test-env")

    # ── 10: API key — file fallback ───────────────────────────────────────────
    with patch.dict(os.environ, {}, clear=True):
        # Remove OPENROUTER_API_KEY if present so the file path is exercised
        os.environ.pop("OPENROUTER_API_KEY", None)
        tmp_dir = Path("/tmp/_a7_test_key_dir")
        tmp_dir.mkdir(exist_ok=True)
        key_file = tmp_dir / "openrouter-key"
        key_file.write_text("sk-file-key\n", encoding="utf-8")
        key = _load_api_key(tmp_dir)
        T.check("api_key: reads from openrouter-key file", key == "sk-file-key")
        key_file.unlink()
        tmp_dir.rmdir()

    # ── 11: API key — missing ─────────────────────────────────────────────────
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OPENROUTER_API_KEY", None)
        key = _load_api_key(Path("/nonexistent"))
        T.check("api_key: returns empty string when not found", key == "")

    # ── 12: JSONL emit format ─────────────────────────────────────────────────
    # Capture inside the block, check outside so T.check() doesn't pollute stdout.
    _emit_captured: str = ""
    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        _emit({"turn": 1, "event": "step", "mode": "autonomous"})
        _emit_captured = mock_stdout.getvalue().strip()
    _emit_parsed = json.loads(_emit_captured)
    T.check("jsonl: emit produces valid JSON", isinstance(_emit_parsed, dict))
    T.check("jsonl: emit preserves keys", _emit_parsed["event"] == "step")
    T.check("jsonl: emit preserves turn", _emit_parsed["turn"] == 1)

    # ── 13: JSONL exit events format ─────────────────────────────────────────
    # Check that all expected event shapes are serialisable
    for event_obj in [
        {
            "turn": 0,
            "event": "session_start",
            "stage_id": "s1",
            "model": "m",
            "manager_max_turns": 25,
        },
        {
            "turn": 1,
            "event": "step",
            "mode": "autonomous",
            "in_flight": [],
            "action": "DISPATCH",
            "node": "auth.jwt",
        },
        {"turn": 2, "event": "halt", "reason": "wild limit reached for auth.jwt"},
        {"turn": 3, "event": "sealed"},
        {"turn": 3, "event": "crashed", "error": "oops", "traceback": "..."},
        {"turn": 3, "event": "turn_limit", "turns_used": 100},
    ]:
        try:
            round_tripped = json.loads(json.dumps(event_obj))
            ok = round_tripped == event_obj
        except Exception:
            ok = False
        T.check(f"jsonl: event shape {event_obj['event']!r} round-trips", ok)

    # ── 14: _run_loop — sealed exit ───────────────────────────────────────────
    # Mock a harness that returns dead state after one step, with sealed stage.
    # Capture stdout inside the block, run all T.check() calls outside.
    from unittest.mock import PropertyMock  # noqa: F401 — imported for completeness

    mock_state_init = MagicMock()
    mock_state_init.turn = 0
    mock_state_init.is_dead = False
    mock_state_init.in_flight = []
    mock_state_init.mode = MagicMock()
    mock_state_init.mode.value = "autonomous"

    mock_state_dead = MagicMock()
    mock_state_dead.turn = 1
    mock_state_dead.is_dead = True
    mock_state_dead.in_flight = []
    mock_state_dead.mode = MagicMock()
    mock_state_dead.mode.value = "dead"

    mock_harness = MagicMock()
    mock_harness.init_session.return_value = mock_state_init
    mock_harness.step.return_value = mock_state_dead
    mock_harness._lifecycle = "new"
    mock_harness.repo.get_events.return_value = [
        {"actor": "harness", "action": "seal", "target": "s1", "turn": 1}
    ]

    _sealed_code: int = -1
    _sealed_out: str = ""
    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        with patch(f"{__name__}._stage_is_sealed", return_value=True):
            fake_args = _RunArgs()
            fake_args.runner_turn_limit = 100
            fake_args.model = DEFAULT_MANAGER_MODEL
            fake_args.subagent_model = DEFAULT_SUBAGENT_MODEL

            _sealed_code = _run_loop(
                harness=mock_harness,
                stage_id="s1",
                args=fake_args,
            )
        _sealed_out = mock_out.getvalue()

    T.check("run_loop: sealed → EXIT_SEALED (0)", _sealed_code == EXIT_SEALED)
    _sealed_lines = [line for line in _sealed_out.splitlines() if line.strip()]
    _sealed_events = [json.loads(line) for line in _sealed_lines]
    T.check(
        "run_loop: sealed — last event is 'sealed'",
        bool(_sealed_events) and _sealed_events[-1]["event"] == "sealed",
    )

    # ── 15: _run_loop — halt exit ─────────────────────────────────────────────
    from a7_rt_core.harness.core import HaltSignal

    mock_harness_halt = MagicMock()
    mock_state_h = MagicMock()
    mock_state_h.turn = 2
    mock_state_h.is_dead = False
    mock_state_h.in_flight = []
    mock_state_h.mode = MagicMock()
    mock_state_h.mode.value = "autonomous"
    mock_state_h.manager_max_turns = 25

    mock_harness_halt.init_session.return_value = mock_state_h
    mock_harness_halt.step.side_effect = HaltSignal("test halt reason")
    mock_harness_halt._lifecycle = "new"
    mock_harness_halt.repo.get_events.return_value = []

    fake_args2 = _RunArgs()
    fake_args2.runner_turn_limit = 100
    fake_args2.model = DEFAULT_MANAGER_MODEL
    fake_args2.subagent_model = DEFAULT_SUBAGENT_MODEL

    _halt_code: int = -1
    _halt_out: str = ""
    with patch("sys.stdout", new_callable=io.StringIO) as mock_out2:
        _halt_code = _run_loop(
            harness=mock_harness_halt,
            stage_id="s1",
            args=fake_args2,
        )
        _halt_out = mock_out2.getvalue()

    T.check("run_loop: HaltSignal → EXIT_HALTED (1)", _halt_code == EXIT_HALTED)
    _halt_lines = [line for line in _halt_out.splitlines() if line.strip()]
    _halt_events = [json.loads(line) for line in _halt_lines]
    _halt_ev = next((e for e in _halt_events if e.get("event") == "halt"), None)
    T.check("run_loop: halt event emitted", _halt_ev is not None)
    T.check(
        "run_loop: halt event carries reason",
        _halt_ev is not None and _halt_ev.get("reason") == "test halt reason",
    )

    # ── 16: _run_loop — turn limit ────────────────────────────────────────────
    mock_state_tl = MagicMock()
    mock_state_tl.turn = 5
    mock_state_tl.is_dead = False
    mock_state_tl.in_flight = []
    mock_state_tl.mode = MagicMock()
    mock_state_tl.mode.value = "autonomous"
    mock_state_tl.manager_max_turns = 25

    mock_harness_tl = MagicMock()
    mock_harness_tl.init_session.return_value = mock_state_tl
    mock_harness_tl._lifecycle = "new"
    mock_harness_tl.repo.get_events.return_value = []

    fake_args3 = _RunArgs()
    fake_args3.runner_turn_limit = 5  # cap at turn 5; state.turn == 5 already → trigger
    fake_args3.model = DEFAULT_MANAGER_MODEL
    fake_args3.subagent_model = DEFAULT_SUBAGENT_MODEL

    _tl_code: int = -1
    _tl_out: str = ""
    with patch("sys.stdout", new_callable=io.StringIO) as mock_out3:
        _tl_code = _run_loop(
            harness=mock_harness_tl,
            stage_id="s1",
            args=fake_args3,
        )
        _tl_out = mock_out3.getvalue()

    T.check("run_loop: turn limit → EXIT_TURN_LIMIT (3)", _tl_code == EXIT_TURN_LIMIT)
    _tl_lines = [line for line in _tl_out.splitlines() if line.strip()]
    _tl_events = [json.loads(line) for line in _tl_lines]
    _tl_ev = next((e for e in _tl_events if e.get("event") == "turn_limit"), None)
    T.check("run_loop: turn_limit event emitted", _tl_ev is not None)

    # ── 17: _run_loop — unexpected crash ──────────────────────────────────────
    mock_state_crash = MagicMock()
    mock_state_crash.turn = 0
    mock_state_crash.is_dead = False
    mock_state_crash.in_flight = []
    mock_state_crash.mode = MagicMock()
    mock_state_crash.mode.value = "autonomous"
    mock_state_crash.manager_max_turns = 25

    mock_harness_crash = MagicMock()
    mock_harness_crash.init_session.return_value = mock_state_crash
    mock_harness_crash.step.side_effect = RuntimeError("kaboom")
    mock_harness_crash._lifecycle = "new"
    mock_harness_crash.repo.get_events.return_value = []

    fake_args4 = _RunArgs()
    fake_args4.runner_turn_limit = 100
    fake_args4.model = DEFAULT_MANAGER_MODEL
    fake_args4.subagent_model = DEFAULT_SUBAGENT_MODEL

    _crash_code: int = -1
    _crash_out: str = ""
    with patch("sys.stdout", new_callable=io.StringIO) as mock_out4:
        _crash_code = _run_loop(
            harness=mock_harness_crash,
            stage_id="s1",
            args=fake_args4,
        )
        _crash_out = mock_out4.getvalue()

    T.check("run_loop: RuntimeError → EXIT_CRASHED (2)", _crash_code == EXIT_CRASHED)
    _crash_lines = [line for line in _crash_out.splitlines() if line.strip()]
    _crash_events = [json.loads(line) for line in _crash_lines]
    _crash_ev = next((e for e in _crash_events if e.get("event") == "crashed"), None)
    T.check("run_loop: crashed event emitted", _crash_ev is not None)
    T.check(
        "run_loop: crashed event has error field",
        _crash_ev is not None and "error" in _crash_ev,
    )
    T.check(
        "run_loop: crashed event has traceback field",
        _crash_ev is not None and "traceback" in _crash_ev,
    )

    # ── 18: _last_harness_action ──────────────────────────────────────────────
    mock_repo = MagicMock()
    mock_repo.get_events.return_value = [
        {"actor": "manager", "action": "DISPATCH", "target": "x.y", "turn": 1},
        {"actor": "harness", "action": "dispatch", "target": "auth.jwt", "turn": 1},
        {"actor": "harness", "action": "checkpoint", "target": "stage-1", "turn": 1},
    ]
    act, tgt = _last_harness_action(mock_repo, min_turn=1)
    T.check("last_harness_action: picks most recent harness event", act == "checkpoint")
    T.check("last_harness_action: extracts target", tgt == "stage-1")

    mock_repo2 = MagicMock()
    mock_repo2.get_events.return_value = []
    act2, tgt2 = _last_harness_action(mock_repo2, min_turn=0)
    T.check(
        "last_harness_action: empty events → (None, None)",
        act2 is None and tgt2 is None,
    )

    # ── 19: _stage_is_sealed ─────────────────────────────────────────────────
    mock_repo3 = MagicMock()
    mock_repo3._load.return_value = {
        "stages": {"s1": {"status": "sealed"}, "s2": {"status": "active"}}
    }
    T.check(
        "_stage_is_sealed: returns True for sealed stage",
        _stage_is_sealed(mock_repo3, "s1") is True,
    )
    T.check(
        "_stage_is_sealed: returns False for active stage",
        _stage_is_sealed(mock_repo3, "s2") is False,
    )
    T.check(
        "_stage_is_sealed: returns False for missing stage",
        _stage_is_sealed(mock_repo3, "nonexistent") is False,
    )

    # ── 20: manager hook — LLM error returns HaltAction ─────────────────────
    # _make_manager_hook must be importable from this module (no Textual dep)
    from a7_rt_core.core.models import HaltAction

    broken_llm = MagicMock()
    broken_llm.call.side_effect = RuntimeError("network down")

    # Provide a real-ish roles_dir that has no manager_prompt.md
    hook = _make_manager_hook(broken_llm, Path("/nonexistent_roles"))
    result = hook({}, MagicMock())
    T.check(
        "manager_hook: LLM error returns HaltAction",
        isinstance(result, HaltAction),
    )
    T.check(
        "manager_hook: HaltAction reason mentions LLM error",
        "manager LLM error" in result.reason,
    )

    _test_new_flags()

    T.summary()
