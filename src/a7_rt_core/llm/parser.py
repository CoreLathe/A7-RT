"""
A7-RT Manager Action Parser

Shared parser for extracting structured ManagerAction objects from raw LLM
text responses. Used by manager_test.py, builder_live_test.py, and the TUI.

The manager is instructed to emit exactly one JSON block after 'ACT: '.
Four extraction strategies are tried in order:
  1. Inline JSON on the same line as 'ACT: '
  2. Fenced code block immediately after 'ACT: '
  3. Any fenced JSON block in the response
  4. First balanced { ... } block containing an "action" key

On any parse failure: returns HaltAction with reason describing the failure.

Phase 6+ migration: Native function calling via submit_action tool.
The legacy parser remains available as fallback during transition.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from a7_rt_core.core.models import (
    ChecklistItem,
    CommitAction,
    ConsultAction,
    DispatchAction,
    HaltAction,
    ManagerAction,
    NodeStatus,
    RedispatchAction,
    SealAction,
    SuspendAction,
    SuspensionType,
    UpdatePlanAction,
    ValidateAction,
)
from a7_rt_core.schemas import load_manager_tool_choice, load_manager_tools

if TYPE_CHECKING:
    from a7_rt_core.llm.client import LLMResponse


# Load manager tool schemas from external JSON for easy editing
MANAGER_TOOLS = load_manager_tools()
MANAGER_TOOL_CHOICE = load_manager_tool_choice()


def parse_manager_action(raw: str) -> ManagerAction:
    """
    Extract and parse the JSON action block from an manager LLM response.

    The manager is instructed to emit exactly one JSON block after 'ACT: '.
    Handles:
      - Inline JSON after 'ACT: '
      - Fenced code blocks (```json ... ```)
      - Fallback: extract first { ... } block from full response

    On any parse failure: returns HaltAction("manager returned malformed JSON").
    """
    # Strategy 1: ACT: { ... } on same line or following lines
    act_match = re.search(r"ACT:\s*(\{.*?\})", raw, re.DOTALL)
    if act_match:
        candidate = act_match.group(1)
        try:
            data = json.loads(candidate)
            return build_action(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Strategy 2: fenced code block after ACT:
    act_fence_match = re.search(r"ACT:.*?```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if act_fence_match:
        candidate = act_fence_match.group(1)
        try:
            data = json.loads(candidate)
            return build_action(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Strategy 3: any fenced JSON block in the response
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            data = json.loads(candidate)
            if "action" in data:
                return build_action(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Strategy 4: first balanced { ... } block containing "action" key
    # Use brace counting to handle nested structures
    candidate = _extract_balanced_json(raw)
    if candidate:
        try:
            data = json.loads(candidate)
            if "action" in data:
                return build_action(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    return HaltAction(
        reason="manager returned malformed JSON — could not extract action block",
        invariant=None,
    )


def _extract_balanced_json(text: str) -> str | None:
    """
    Extract the first balanced JSON object from text using brace counting.
    Handles nested objects and arrays.
    """
    start = text.find("{")
    if start == -1:
        return None

    brace_count = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:]):
        idx = start + i

        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if not in_string:
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    return text[start : idx + 1]

    return None


def build_action(data: dict) -> ManagerAction:
    """
    Construct the appropriate ManagerAction subclass from a parsed dict.
    Raises ValueError on unknown or malformed action.
    """
    action_name = str(data.get("action", "")).upper()

    if action_name == "DISPATCH":
        action = DispatchAction(
            node_id=data["node_id"],
            role=data["role"],
            weight=data.get("weight", "lean"),
            scope=data.get("scope"),  # Analyst scope: node|project|external
            query=data.get("query"),  # Analyst query string
            target_nodes=data.get("target_nodes"),  # Analyst target nodes (project scope)
        )
        action.intent = data.get("intent")
        return action
    elif action_name == "REDISPATCH":
        action = RedispatchAction(
            node_id=data["node_id"],
            role=data["role"],
            weight=data.get("weight", "lean"),
            manager_note=data.get("manager_note", ""),
        )
        action.intent = data.get("intent")
        return action
    elif action_name == "VALIDATE":
        action = ValidateAction(node_id=data["node_id"])
        action.intent = data.get("intent")
        return action
    elif action_name == "COMMIT":
        status_str = str(data.get("status", "") or "grounded").lower()
        status = NodeStatus(status_str)
        action = CommitAction(
            node_id=data["node_id"],
            status=status,
            reason=data.get("reason", ""),
            manager_note=data.get("manager_note"),
        )
        action.intent = data.get("intent")
        return action
    elif action_name == "CONSULT":
        action = ConsultAction(
            question=data["question"],
            relevant_node_ids=data.get("relevant_node_ids", []),
            constraints=data.get("constraints", []),
        )
        action.intent = data.get("intent")
        return action
    elif action_name == "SUSPEND":
        suspension_type = SuspensionType(str(data.get("type", "wild")).lower())
        action = SuspendAction(
            node_id=data["node_id"],
            type=suspension_type,
            detail=data.get("detail", ""),
        )
        action.intent = data.get("intent")
        return action
    elif action_name == "SEAL":
        action = SealAction(summary=data.get("summary", ""))
        action.intent = data.get("intent")
        return action
    elif action_name == "HALT":
        action = HaltAction(
            reason=data.get("reason", "unknown"),
            invariant=data.get("invariant"),
        )
        action.intent = data.get("intent")
        return action
    elif action_name == "UPDATE_PLAN":
        checklist_data = data.get("checklist", [])
        checklist = []
        for item in checklist_data:
            import uuid

            checklist.append(
                ChecklistItem(
                    id=item.get("id") or str(uuid.uuid4())[:8],
                    text=item["text"],
                    status=item.get("status", "pending"),
                )
            )
        action = UpdatePlanAction(
            narrative=data.get("narrative"),
            checklist=checklist if checklist else None,
        )
        action.intent = data.get("intent")
        return action
    else:
        raise ValueError(f"Unknown action: {action_name!r}")


def parse_manager_tool_call(tool_call: dict) -> ManagerAction:
    """
    Convert native tool call to ManagerAction via existing harness pipeline.

    Flow: tool_call → reconstruct JSON → build_action() → harness validation

    Parameters
    ----------
    tool_call : dict
        OpenAI format tool call with keys: id, name, arguments (JSON string)

    Returns
    -------
    ManagerAction : subclass instance (or HaltAction on error)
    """
    try:
        name = tool_call.get("name") or tool_call.get("function", {}).get("name")
        args_str = (
            tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or "{}"
        )

        if name != "submit_action":
            return HaltAction(
                reason=f"Unexpected tool call: {name!r} (expected submit_action)",
                invariant=None,
            )

        args = json.loads(args_str)

        # Reconstruct legacy format that build_action() expects
        data = {"action": args.get("action_type"), **args.get("parameters", {})}

        return build_action(data)

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return HaltAction(
            reason=f"Invalid action structure in tool call: {e}",
            invariant=None,
        )


def handle_manager_response(response: "LLMResponse") -> ManagerAction:
    """
    Route manager LLM response through appropriate parser.

    Priority: If tool_calls present, use native adapter exclusively.
    Do not fall back to content parsing — avoids ambiguity when model
    emits both text and a tool call.

    Parameters
    ----------
    response : LLMResponse
        Structured response with content, tool_calls, finish_reason

    Returns
    -------
    ManagerAction : subclass from tool call or legacy text parser
    """
    if response.tool_calls:
        return parse_manager_tool_call(response.tool_calls[0])

    return parse_manager_action(response.content)
