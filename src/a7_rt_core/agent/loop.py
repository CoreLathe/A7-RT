"""
A7-RT Agent Loop (Native Tool Calling)

Phase 4: Agent uses OpenAI native tool calling for exploration and submission.
- No XML parsing
- No conversation flattening
- Proper multi-turn message handling
- Tool schemas loaded from external JSON for easy editing
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from a7_rt_core.core.models import (
    IterationStatus,
    NodeStatus,
    SubagentReturn,
    SuspensionReason,
    SuspensionType,
)
from a7_rt_core.llm.client import LLMClient, LLMError
from a7_rt_core.schemas import load_agent_tools
from a7_rt_core.tools.agent_tools import AgentTools, ToolError

# Load tool schemas from external JSON file for easy editing
TOOL_SCHEMAS = load_agent_tools()

# Module-level logger
logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Stateful agent that runs multiple turns using native tool calling.

    Uses OpenAI tool calling API for:
    - Exploration: read_file, grep_content, list_files
    - Submission: submit_pr with structured JSON
    """

    def __init__(
        self,
        llm: LLMClient,
        content_dir: str | Path,
        roles_dir: str | Path | None = None,
        max_tokens: int = 4096,
        context_mode: str = "accumulate",
    ) -> None:
        self._llm = llm
        self._tools = AgentTools(content_dir)
        self._max_tokens = max_tokens
        self._context_mode = context_mode
        self._roles_dir = Path(roles_dir) if roles_dir else None
        self._role_prompts: dict[str, str] = {}
        self._load_role_prompts()

    def run(
        self,
        node_id: str,
        role: str,
        ctx: dict[str, Any],
        expected_iterations: int = 5,
        checkpoint_interval: int = 10,  # Telemetry checkpoint, not hard limit
        progress_threshold: int = 3,  # Redispatch if no progress for N turns
        session_id: str | None = None,
        on_tool_call: Callable | None = None,
        shadow: Any | None = None,
        previous_thoughts: list[dict] | None = None,  # Persisted reasoning
    ) -> SubagentReturn:
        """
        Run the agent loop until completion or checkpoint.

        Fresh-context redispatch architecture:
        - No max_iterations hard limit; runs until completion or checkpoint
        - Returns to harness at checkpoint_interval for decision
        - Tracks progress to detect stalls

        Args:
            node_id: The node being worked on
            role: Subagent role (builder, test_author, analyst)
            ctx: Context view from context.py
            expected_iterations: Soft guidance on anticipated difficulty
            checkpoint_interval: Telemetry checkpoint interval (not hard limit)
            progress_threshold: Redispatch if no progress for N turns
            session_id: UUID for this dispatch session
            on_tool_call: Callback(tool_name, args, turn, intent) -> None
            shadow: ShadowFS instance for transactional file operations
            previous_thoughts: Persisted reasoning from prior dispatches

        Returns:
            SubagentReturn with status, files, and telemetry
        """
        # Reset tool usage at start of new dispatch
        self._tools.reset_usage()

        # Attach shadow to tools if provided
        if shadow:
            self._tools.attach_shadow(shadow, node_id)

        # Load previous thoughts if provided
        if previous_thoughts:
            for thought in previous_thoughts:
                self._tools._thoughts.append(thought)

        # Progress tracking state
        iteration = 0
        last_progress_iteration = 0
        files_written_at_start = set(self._tools.get_shadow_writes().keys()) if shadow else set()
        tests_passing = False
        tests_failing = False

        # Current state for fresh context building (Phase 3)
        current_state: dict[str, Any] = {
            "files_written": [],
            "files_read": [],
            "last_test_result": None,
            "thoughts_recorded": [],
        }

        # Initialize messages based on context mode
        if self._context_mode == "accumulate":
            # Legacy mode: build once, append tool results
            messages = self._build_messages(role, ctx, expected_iterations)
        else:
            # Fresh mode: messages rebuilt each iteration
            messages = []

        # Circuit breaker state for API failures
        consecutive_api_failures = 0
        max_consecutive_failures = 3
        api_failure_pause_seconds = 30

        while True:
            iteration += 1

            # Check for checkpoint interval only in fresh mode
            if self._context_mode == "fresh" and iteration % checkpoint_interval == 0:
                # Determine iteration status
                current_files = set(self._tools.get_shadow_writes().keys()) if shadow else set()
                files_changed = len(current_files - files_written_at_start) > 0

                if tests_passing:
                    iter_status = IterationStatus.TEST_PASSING
                elif tests_failing:
                    iter_status = IterationStatus.TEST_FAILING
                elif files_changed or (iteration - last_progress_iteration) <= progress_threshold:
                    iter_status = IterationStatus.PROGRESS
                else:
                    iter_status = IterationStatus.STALLED

                return SubagentReturn(
                    status=NodeStatus.SUSPENDED,
                    suspension_reason=SuspensionReason(
                        type=SuspensionType.WILD,
                        detail=f"Checkpoint reached at iteration {iteration}",
                    ),
                    iterations_used=iteration,
                    tool_usage=self._tools.get_usage(),
                    role=role,
                    session_id=session_id,
                    checkpoint_reached=True,
                    iteration_status=iter_status,
                    thoughts_recorded=self._tools.get_thoughts(),
                )

            # Build messages based on context mode
            if self._context_mode == "fresh":
                # Phase 3: Fresh context each iteration (no conversation accumulation)
                messages = self._build_fresh_messages(role, ctx, current_state, iteration)

            # Call LLM with tools available
            try:
                response = self._llm.call_with_messages(
                    messages=messages,
                    max_tokens=self._max_tokens,
                    tools=TOOL_SCHEMAS,
                )
                # Reset circuit breaker on success
                consecutive_api_failures = 0
            except LLMError as e:
                consecutive_api_failures += 1

                # Circuit breaker: if we've failed multiple times, pause and retry
                if consecutive_api_failures < max_consecutive_failures:
                    logger.warning(
                        "API failure %d/%d at iteration %d: %s. Pausing %ds before retry...",
                        consecutive_api_failures,
                        max_consecutive_failures,
                        iteration,
                        e,
                        api_failure_pause_seconds,
                    )
                    time.sleep(api_failure_pause_seconds)
                    # Decrement iteration since we're retrying (don't count toward limit)
                    iteration -= 1
                    continue

                # Circuit breaker triggered: too many consecutive failures
                return SubagentReturn(
                    status=NodeStatus.SUSPENDED,
                    suspension_reason=SuspensionReason(
                        type=SuspensionType.WILD,
                        detail=f"LLM API error at iteration {iteration} after {max_consecutive_failures} retries: {e}",
                    ),
                    iterations_used=iteration,
                    tool_usage=self._tools.get_usage(),
                    role=role,
                    session_id=session_id,
                    checkpoint_reached=True,
                    iteration_status=IterationStatus.STALLED,
                    thoughts_recorded=self._tools.get_thoughts(),
                )

            # Check for truncation
            if response.finish_reason == "length":
                self._log_raw_response(node_id, role, iteration, response.content, "truncation")
                return SubagentReturn(
                    status=NodeStatus.SUSPENDED,
                    suspension_reason=SuspensionReason(
                        type=SuspensionType.WILD,
                        detail=f"Response truncated at iteration {iteration}. "
                        "Max tokens insufficient for this task.",
                    ),
                    iterations_used=iteration,
                    tool_usage=self._tools.get_usage(),
                    role=role,
                    session_id=session_id,
                    checkpoint_reached=True,
                    iteration_status=IterationStatus.STALLED,
                    thoughts_recorded=self._tools.get_thoughts(),
                )

            # Handle tool calls
            if response.tool_calls:
                submit_pr_failed = False
                escalate_triggered = False

                for tool_call in response.tool_calls:
                    if tool_call["name"] == "submit_pr":
                        # Parse submission and validate
                        result = self._parse_submit_pr(
                            tool_call["arguments"],
                            node_id,
                            role,
                            iteration,
                            session_id=session_id,
                            ctx=ctx,
                        )
                        # Check if escalate flag is set
                        if result.escalate:
                            escalate_triggered = True
                            break
                        # Check if this is a validation error that should trigger retry
                        if (
                            result.status == NodeStatus.SUSPENDED
                            and result.suspension_reason
                            and "ERROR: test_contract REQUIRED" in result.suspension_reason.detail
                        ):
                            # Validation failed - update state and continue
                            current_state["last_error"] = result.suspension_reason.detail
                            submit_pr_failed = True
                            break  # Exit for loop, will continue outer while
                        # Valid submission - add checkpoint/iteration status fields
                        result.checkpoint_reached = False
                        result.iteration_status = None
                        result.thoughts_recorded = self._tools.get_thoughts()
                        return result

                if escalate_triggered:
                    # Return with escalation status for manager review
                    return SubagentReturn(
                        status=NodeStatus.SUSPENDED,
                        suspension_reason=SuspensionReason(
                            type=SuspensionType.WILD,
                            detail=f"Agent requested escalation at iteration {iteration}",
                        ),
                        iterations_used=iteration,
                        tool_usage=self._tools.get_usage(),
                        role=role,
                        session_id=session_id,
                        checkpoint_reached=True,
                        iteration_status=IterationStatus.STALLED,
                        thoughts_recorded=self._tools.get_thoughts(),
                    )

                if submit_pr_failed:
                    continue  # Continue to next while iteration

                # Execute exploration tools with callback
                tool_results = self._execute_tool_calls(
                    response.tool_calls,
                    turn=iteration,
                    on_tool_call=on_tool_call,
                )

                # Extract state from tool results
                current_state = self._extract_state_from_results(tool_results, current_state)

                # Track progress from tool results
                for result in tool_results:
                    tool_name = result.get("tool")
                    if tool_name in ("write_file", "edit_file", "edit_files"):
                        last_progress_iteration = iteration
                    if tool_name == "run_test":
                        test_result = result.get("result", {})
                        if test_result.get("passed"):
                            tests_passing = True
                            tests_failing = False
                        else:
                            tests_failing = True

                # Handle message accumulation based on context mode
                if self._context_mode == "accumulate":
                    # Legacy mode: append assistant message and tool results
                    assistant_msg = {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            }
                            for tc in response.tool_calls
                        ],
                    }
                    if response.reasoning_content:
                        assistant_msg["reasoning_content"] = response.reasoning_content
                    messages.append(assistant_msg)

                    # Append tool results - match each result to its tool_call_id
                    # tool_results are in the same order as response.tool_calls
                    for i, result in enumerate(tool_results):
                        if i < len(response.tool_calls):
                            tool_call_id = response.tool_calls[i]["id"]
                        else:
                            # Fallback shouldn't happen, but be safe
                            tool_call_id = (
                                response.tool_calls[0]["id"] if response.tool_calls else "unknown"
                            )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": json.dumps(result.get("result", {})),
                            }
                        )
                else:
                    # Fresh mode: Loop continues without message accumulation
                    pass

                continue

            # No tool calls and no submission - prompt to continue
            # Phase 3: Fresh context will include this guidance next iteration

    def _build_fresh_messages(
        self,
        role: str,
        ctx: dict[str, Any],
        current_state: dict[str, Any],
        iteration: int,
    ) -> list[dict[str, Any]]:
        """
        Build fresh message context for each iteration (Phase 3).

        No conversation accumulation - each turn is independent.
        The agent re-reads files as needed rather than relying on history.

        Includes:
        - System prompt (role definition)
        - User context (contract, target node, current state summary)
        """
        system = self._build_system_prompt(role, ctx)

        # Build user message with current state
        target = ctx.get("target", {})
        node_id = target.get("node_id", "unknown")

        user_parts = [f"Node: {node_id}"]
        user_parts.append(f"Iteration: {iteration}")

        # Current state summary
        files_written = current_state.get("files_written", [])
        if files_written:
            user_parts.append(f"Files written: {', '.join(files_written)}")

        files_read = current_state.get("files_read", [])
        if files_read:
            user_parts.append(f"Files read: {', '.join(files_read[-5:])}")  # Last 5

        last_test = current_state.get("last_test_result")
        if last_test:
            passed = last_test.get("passed", False)
            user_parts.append(f"Last test: {'PASSED' if passed else 'FAILED'}")
            if not passed and last_test.get("failure_count"):
                user_parts.append(f"Test failures: {last_test['failure_count']}")
            if last_test.get("key_failures"):
                user_parts.append(f"Key failures: {len(last_test['key_failures'])}")

        thoughts = current_state.get("thoughts_recorded", [])
        if thoughts:
            user_parts.append(f"Thoughts recorded: {len(thoughts)}")

        if current_state.get("last_error"):
            user_parts.append(f"Previous error: {current_state['last_error'][:200]}")

        # Status guidance with explicit NEXT ACTION
        if not files_written:
            user_parts.append(
                "\nStatus: No files written yet.\n"
                "NEXT ACTION: Use write_file() to create the implementation.\n"
                "Do NOT check state again. Write the file now."
            )
        elif last_test and last_test.get("passed"):
            user_parts.append(
                "\nStatus: Tests passing.\nNEXT ACTION: Call submit_pr() to complete this node."
            )
        elif last_test:
            user_parts.append(
                "\nStatus: Tests failing.\n"
                "NEXT ACTION: Read the test output, identify the failure, edit the implementation, re-run tests."
            )
        else:
            user_parts.append(
                "\nStatus: Implementation written but not tested.\n"
                "NEXT ACTION: Call run_test() to verify the implementation."
            )

        # Include full context (contract, exports, guarantees, deps)
        # Phase 3: Fresh context includes everything the agent needs
        ctx_json = json.dumps(ctx, indent=2, default=str)
        user_parts.append(f"\n---\nFull Context:\n{ctx_json}")

        user_content = "\n".join(user_parts)

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

    def _extract_state_from_results(
        self,
        tool_results: list[dict],
        previous_state: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Extract relevant state from tool execution results (Phase 3).

        Tracks only what the agent needs to know for next iteration,
        not full conversation history.
        """
        state = {
            "files_written": previous_state.get("files_written", []).copy(),
            "files_read": previous_state.get("files_read", []).copy(),
            "last_test_result": previous_state.get("last_test_result"),
            "thoughts_recorded": previous_state.get("thoughts_recorded", []).copy(),
            "last_error": previous_state.get("last_error"),
        }

        for result in tool_results:
            tool_name = result.get("tool")
            tool_result = result.get("result", {})
            arguments = result.get("arguments", {})

            if tool_name == "write_file" and result.get("success"):
                path = tool_result.get("path") if isinstance(tool_result, dict) else None
                if path and path not in state["files_written"]:
                    state["files_written"].append(path)

            elif tool_name == "read_file" and result.get("success"):
                path = arguments.get("path")
                if path and path not in state["files_read"]:
                    state["files_read"].append(path)

            elif tool_name == "run_test":
                # Capture test result with full output for diagnosis
                if isinstance(tool_result, dict):
                    output = tool_result.get("output", "")
                    # Truncate if extremely long, but keep enough for diagnosis
                    max_output = 3000
                    if len(output) > max_output:
                        output = output[:max_output] + "\n... [truncated]"
                    state["last_test_result"] = {
                        "passed": tool_result.get("passed", False),
                        "output": output,
                        "exit_code": tool_result.get("exit_code", -1),
                        "summary": tool_result.get("summary", ""),
                    }

            elif tool_name == "record_thought":
                thought = arguments.get("thought")
                if thought:
                    state["thoughts_recorded"].append(
                        {
                            "iteration": result.get("iteration"),
                            "thought": thought,
                        }
                    )

        return state

    def _build_context_summary(self, ctx: dict[str, Any]) -> str:
        """Build a concise summary of the context for fresh messages."""
        parts = []

        target = ctx.get("target", {})
        if target.get("description"):
            parts.append(f"Description: {target['description']}")

        interface = target.get("interface", {})
        if interface.get("exports"):
            parts.append(f"Exports: {', '.join(interface['exports'][:5])}")  # First 5

        # Previous attempt info from Phase 2
        prev_attempt = ctx.get("previous_attempt")
        if prev_attempt:
            parts.append(f"\nPrevious attempt (#{prev_attempt.get('dispatch_number')}):")
            if prev_attempt.get("guidance"):
                parts.append(f"  Guidance: {prev_attempt['guidance']}")
            test_summary = prev_attempt.get("test_summary", {})
            if test_summary.get("key_failures"):
                parts.append(f"  Previous failures: {len(test_summary['key_failures'])}")

        return "\n".join(parts)

    def _build_messages(
        self,
        role: str,
        ctx: dict[str, Any],
        expected_iterations: int,
    ) -> list[dict[str, Any]]:
        """Build initial message list for the conversation (legacy, kept for compatibility)."""
        system = self._build_system_prompt(role, ctx)
        user = self._build_user_message(ctx, expected_iterations)

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_system_prompt(self, role: str, ctx: dict) -> str:
        """Build system prompt for the agent."""
        parts = []

        # Load base role prompt from file if available
        role_prompt = self._get_role_prompt(role)
        if role_prompt:
            parts.append(role_prompt)
        else:
            # Fallback minimal prompts if files not available
            protocol_weight = ctx.get("target", {}).get("protocol_weight", "lean")
            if protocol_weight in ("lean", "full"):
                parts.append(f"[A7 Protocol: {protocol_weight}]")

            if role == "builder":
                parts.append(
                    "You are a builder subagent. Implement the target node.\n\n"
                    "WORKFLOW: 1) Explore with read_file/grep_content, 2) Write files with write_file(), 3) Call submit_pr().\n\n"
                    "INVARIANT: When status='grounded', the 'files' parameter MUST contain non-empty file content. "
                    "The system REJECTS grounded submissions without files. "
                    "You MUST call write_file() with actual code before submit_pr(). "
                    "Empty files or missing files = automatic rejection."
                )
            elif role == "test_author":
                parts.append(
                    "You are a test author subagent. Write comprehensive tests. "
                    "Use tools to understand the interface. "
                    "CRITICAL: submit_pr is your final action — everything after is discarded. "
                    "Name your test file '{node_id}.test' (e.g., 'types.core.test' for node 'types.core'). "
                    "Use dot separators, not underscores. "
                    "When complete, call submit_pr with your test files."
                )
            elif role == "analyst":
                parts.append(
                    "You are an analyst subagent. Analyze the context and call submit_pr "
                    "with your findings. Analyst does not use exploration tools."
                )

        # Dynamic fragment: Redispatch guidance
        exploration_hints = ctx.get("exploration_hints", {})
        if exploration_hints.get("previous_attempts", 0) > 0:
            files_written = exploration_hints.get("files_written", [])
            files_msg = (
                f"Files produced: {', '.join(files_written)}"
                if files_written
                else "No files produced yet."
            )
            parts.append(
                "\n---\n**REDISPATCH NOTICE:** This node has been dispatched "
                f"{exploration_hints['previous_attempts']} time(s) before. "
                f"{files_msg} "
                "Review prior work and address any issues in this attempt."
            )

        # Dynamic fragment: Intent guidance
        parts.append(
            "\n---\n**TIP:** Use the 'intent' parameter on tool calls to record your "
            "reasoning. This helps future attempts understand your thought process. "
            "Examples: {'path': 'auth/jwt.py', 'intent': 'Understanding JWT verification'} "
            "or {'pattern': 'def verify', 'intent': 'Finding the verify function'}"
        )

        return "\n\n".join(parts)

    def _build_user_message(self, ctx: dict, expected_iterations: int) -> str:
        """Build the initial user message with context."""
        parts = []

        target = ctx.get("target", {})
        node_id = target.get("node_id", "unknown")
        parts.append(f"Node to implement: {node_id}")

        if expected_iterations > 1:
            parts.append(
                f"\nEstimated difficulty: {expected_iterations} iterations. "
                "Explore thoroughly before submitting."
            )

        # Add manager guidance if present
        manager_guidance = ctx.get("manager_guidance")
        if manager_guidance:
            parts.append("\n---\nManager Guidance:")
            if "note" in manager_guidance:
                parts.append(f"Note: {manager_guidance['note']}")
            if "focus_on" in manager_guidance:
                parts.append(f"Focus on: {', '.join(manager_guidance['focus_on'])}")
            if "previews" in manager_guidance:
                parts.append("Previews:")
                for nid, preview in manager_guidance["previews"].items():
                    parts.append(f"  - {nid}: {preview}")

        # Add explicit workflow guidance
        parts.append(
            "\n---\nWORKFLOW: 1) Read context/contract, 2) Write implementation, "
            "3) Run tests, 4) Submit PR. Do NOT loop on state checks."
        )

        # Add context
        ctx_json = json.dumps(ctx, indent=2, default=str)
        parts.append(f"\n---\nContext:\n{ctx_json}")

        return "\n".join(parts)

    def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        turn: int = 0,
        on_tool_call: Callable | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute tool calls and return structured results (Phase 3).

        Returns list of result dicts with:
        - tool: tool name
        - arguments: original arguments
        - result: execution result
        - success: bool
        - iteration: turn number
        """
        results = []

        for tc in tool_calls:
            name = tc["name"]
            args_str = tc["arguments"]

            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                results.append(
                    {
                        "tool": name,
                        "arguments": {},
                        "result": {"error": "Invalid JSON arguments"},
                        "success": False,
                        "iteration": turn,
                    }
                )
                continue

            # Extract intent from args if provided
            tool_intent = args.get("intent")

            # Call callback if provided
            if on_tool_call:
                try:
                    on_tool_call(name, args, turn, tool_intent)
                except Exception:
                    pass  # Callback errors should not break the loop

            try:
                if name == "read_file":
                    path = args.get("path", "")
                    content = self._tools.read_file(path)
                    result = {"path": path, "content": content}

                elif name == "grep_content":
                    pattern = args.get("pattern", "")
                    matches = self._tools.grep_content(pattern)
                    result = {"pattern": pattern, "matches": matches}

                elif name == "list_files":
                    glob = args.get("glob", "**/*")
                    files = self._tools.list_files(glob)
                    result = {"glob": glob, "files": files}

                elif name == "preview_file":
                    path = args.get("path", "")
                    offset = args.get("offset", 0)
                    max_lines = args.get("max_lines", 50)
                    result = self._tools.preview_file(path, offset, max_lines)

                elif name == "write_file":
                    path = args.get("path", "")
                    content = args.get("content", "")
                    result = self._tools.write_file(path, content)

                elif name == "delete_file":
                    path = args.get("path", "")
                    result = self._tools.delete_file(path)

                elif name == "rename_file" or name == "mv":
                    old_path = args.get("old_path", "")
                    new_path = args.get("new_path", "")
                    result = self._tools.rename_file(old_path, new_path)

                elif name == "run_test":
                    result = self._tools.run_test()

                elif name == "record_thought":
                    thought = args.get("thought", "")
                    category = args.get("category", "hypothesis")
                    relates_to = args.get("relates_to")
                    result = self._tools.record_thought(thought, category, relates_to)

                elif name == "edit_file":
                    path = args.get("path", "")
                    content_hash = args.get("content_hash", "")
                    operations = args.get("operations", [])
                    result = self._tools.edit_file(path, content_hash, operations)

                elif name == "edit_files":
                    edits = args.get("edits", [])
                    result = self._tools.edit_files(edits)

                elif name == "run_lint":
                    paths = args.get("paths")
                    language = args.get("language")
                    result = self._tools.run_lint(paths, language)

                elif name == "get_current_state":
                    result = self._tools.get_current_state()

                elif name == "create_directory":
                    path = args.get("path", "")
                    result = self._tools.create_directory(path)

                else:
                    result = {"error": f"Unknown tool: {name}"}

                results.append(
                    {
                        "tool": name,
                        "arguments": args,
                        "result": result,
                        "success": True,
                        "iteration": turn,
                    }
                )

            except ToolError as e:
                results.append(
                    {
                        "tool": name,
                        "arguments": args,
                        "result": {"error": e.message},
                        "success": False,
                        "iteration": turn,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "tool": name,
                        "arguments": args,
                        "result": {"error": str(e)},
                        "success": False,
                        "iteration": turn,
                    }
                )

        return results

    def _parse_submit_pr(
        self,
        arguments: str,
        node_id: str,
        role: str,
        iterations_used: int,
        session_id: str | None = None,
        ctx: dict[str, Any] | None = None,
    ) -> SubagentReturn:
        """Parse the submit_pr tool call arguments into SubagentReturn."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return self._error_return(f"Invalid JSON in submit_pr: {e}", role, session_id)

        # Log submit_pr call for debugging - captures what agent actually submitted
        files_submitted = list(args.get("files", {}).keys()) if args.get("files") else []
        self._log_session_event(
            node_id,
            {
                "type": "submit_pr",
                "session_id": session_id,
                "status_requested": args.get("status", "provisional"),
                "files_submitted": files_submitted,
                "file_count": len(files_submitted),
                "has_content": bool(args.get("files")),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        status_str = args.get("status", "provisional")
        try:
            status = NodeStatus(status_str)
        except ValueError:
            status = NodeStatus.PROVISIONAL

        # Build interface update
        interface = args.get("interface", {})
        interface_update = {
            "exports": interface.get("exports", []),
            "assumptions": interface.get("assumptions", []),
        }

        # Extract files from submit_pr arguments
        files = args.get("files", {})
        if files and isinstance(files, dict):
            files_dict = {k: v for k, v in files.items() if isinstance(v, str)}
        else:
            files_dict = None

        # Autofill from shadow layer if no files provided
        # This ensures write_file() calls are automatically captured
        if not files_dict:
            shadow_files = self._tools.get_shadow_writes()
            if shadow_files:
                files_dict = shadow_files

        # Validate grounded status has non-empty files
        if status == NodeStatus.GROUNDED and not files_dict:
            error_msg = (
                "INVARIANT VIOLATION: status='grounded' requires non-empty 'files'. "
                "You must write implementation files using write_file() before submit_pr(). "
                "Current files parameter is empty or missing. "
                "Write your code first, then call submit_pr() with status='grounded'."
            )
            return self._error_return(error_msg, role, session_id)

        # Extract file tags
        file_tags = []
        for tag in args.get("file_tags", []):
            if isinstance(tag, dict):
                file_tags.append(
                    {
                        "node_id": tag.get("node_id", ""),
                        "category": tag.get("category", "quirk"),
                        "content": str(tag.get("content", ""))[:200],
                        "propagate": tag.get("propagate", True),
                    }
                )

        # Handle suspension
        suspension_reason = None
        if status == NodeStatus.SUSPENDED:
            sr = args.get("suspension_reason", {})
            suspension_reason = SuspensionReason(
                type=SuspensionType(sr.get("type", "wild")),
                detail=sr.get("detail", "No detail provided"),
            )

        # Extract and truncate pr_note to 1000 chars max (soft guidance: 500)
        pr_note = args.get("pr_note")
        if pr_note is not None:
            pr_note = str(pr_note)[:1000]

        # Extract test_contract from test_author (for builder transit)
        test_contract = args.get("test_contract")
        if test_contract is not None:
            test_contract = str(test_contract)[:1000]

        # Extract analysis_result from analyst
        analysis_result = args.get("analysis_result")

        # Validate test_contract for test_author in Mode B (exports empty)
        # Mode B = original node exports were empty (from ctx), regardless of what was submitted
        # Note: test_author view has "interface" at top level, not nested under "target"
        original_exports = []
        if ctx:
            # test_author view: ctx["interface"]["exports"]
            if "interface" in ctx:
                original_exports = ctx["interface"].get("exports", [])
            # builder view fallback: ctx["target"]["interface"]["exports"]
            elif "target" in ctx and isinstance(ctx["target"], dict):
                original_exports = ctx["target"].get("interface", {}).get("exports", [])
        is_mode_b = len(original_exports) == 0
        if role == "test_author" and is_mode_b and not test_contract:
            # This is Mode B without test_contract - REJECT and require retry
            error_msg = (
                "ERROR: test_contract REQUIRED in Mode B. "
                "The builder cannot see test files and cannot see pr_note. "
                "test_contract is your ONLY communication channel to the builder. "
                "Submit again with test_contract populated per TEST CONTRACT FORMAT section."
            )
            return self._error_return(error_msg, role, session_id)

        return SubagentReturn(
            status=status,
            files=files_dict,
            content=None,
            edits=[],
            interface_update=interface_update,
            file_tags=file_tags,
            escalate=args.get("escalate", False),
            suspension_reason=suspension_reason,
            iterations_used=iterations_used,
            tool_usage=self._tools.get_usage(),
            role=role,
            session_id=session_id,
            pr_note=pr_note,
            test_contract=test_contract,
            test_runs=self._tools.get_test_runs(),
            analysis_result=analysis_result,
        )

    def _error_return(
        self,
        detail: str,
        role: str,
        session_id: str | None = None,
    ) -> SubagentReturn:
        """Create a suspension return for errors."""
        return SubagentReturn(
            status=NodeStatus.SUSPENDED,
            suspension_reason=SuspensionReason(
                type=SuspensionType.WILD,
                detail=detail,
            ),
            iterations_used=0,
            tool_usage=self._tools.get_usage(),
            role=role,
            session_id=session_id,
            checkpoint_reached=True,
            iteration_status=IterationStatus.STALLED,
            thoughts_recorded=self._tools.get_thoughts(),
        )

    def _log_session_event(self, node_id: str, entry: dict) -> None:
        """Log an event to the node's session log (if content_dir available)."""
        if not self._tools._content_dir:
            return
        try:
            sessions_dir = self._tools._content_dir / ".sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            log_path = sessions_dir / f"{node_id}.jsonl"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Logging errors should not break the loop

    def _log_raw_response(
        self,
        node_id: str,
        role: str,
        iteration: int,
        response: str,
        reason: str,
    ) -> None:
        """Log raw response to file for debugging."""
        try:
            log_dir = Path("/tmp/a7_debug")
            log_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{node_id}_{role}_iter{iteration}_{reason}_{timestamp}.log"
            log_path = log_dir / filename

            log_path.write_text(
                f"Node: {node_id}\n"
                f"Role: {role}\n"
                f"Iteration: {iteration}\n"
                f"Reason: {reason}\n"
                f"Response length: {len(response)}\n"
                f"{'=' * 80}\n"
                f"{response}\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_role_prompts(self) -> None:
        """Load role prompts from roles_dir if available."""
        if not self._roles_dir:
            return

        role_files = {
            "builder": "builder.md",
            "test_author": "test_author.md",
            "analyst": "analyst.md",
        }

        for role, filename in role_files.items():
            prompt_path = self._roles_dir / filename
            if prompt_path.exists():
                try:
                    self._role_prompts[role] = prompt_path.read_text(encoding="utf-8")
                except Exception:
                    pass

    def _get_role_prompt(self, role: str) -> str | None:
        """Get cached role prompt or None."""
        return self._role_prompts.get(role)
