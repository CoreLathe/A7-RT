"""
A7-RT Schema Registry

Centralized schema loading for tool definitions, validation rules, and
state transition tables. Schemas are stored as JSON for easy editing and
hot-reloading without code changes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Schema directory resolution
_SCHEMA_DIR = Path(__file__).parent


def _load_json(path: Path) -> Any:
    """Load and parse a JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Schema file not found: {path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in schema file {path}: {e}")
        raise


# -----------------------------------------------------------------------------
# Agent Schemas
# -----------------------------------------------------------------------------


def load_agent_tools() -> list[dict[str, Any]]:
    """
    Load agent tool schemas for OpenAI function calling.

    Returns list of 13 tool definitions:
    - read_file, grep_content, list_files, preview_file
    - write_file, delete_file, rename_file
    - run_test, run_lint
    - edit_file, edit_files
    - record_thought
    - submit_pr
    """
    data = _load_json(_SCHEMA_DIR / "agent" / "tools.json")
    return data["tools"]


# -----------------------------------------------------------------------------
# Manager Schemas
# -----------------------------------------------------------------------------


def load_manager_tools() -> list[dict[str, Any]]:
    """
    Load manager tool schemas.

    Returns list containing submit_action tool definition.
    """
    data = _load_json(_SCHEMA_DIR / "manager" / "tools.json")
    return data["tools"]


def load_manager_tool_choice() -> dict[str, Any]:
    """
    Load manager tool choice forcing config.

    Forces the model to call submit_action.
    """
    data = _load_json(_SCHEMA_DIR / "manager" / "tools.json")
    return data["tool_choice"]


# -----------------------------------------------------------------------------
# Harness Internal Schemas
# -----------------------------------------------------------------------------


def load_validation_rules() -> dict[str, Any]:
    """
    Load subagent return validation rules.

    Returns:
        - required_fields: per-role required field lists
        - nonnull_fields: per-role non-null field lists
        - role_descriptions: human-readable role info
    """
    return _load_json(_SCHEMA_DIR / "harness" / "validation.json")


def load_state_transitions() -> dict[str, Any]:
    """
    Load node status transition rules.

    Returns transition table with valid_next states for each status.
    """
    return _load_json(_SCHEMA_DIR / "harness" / "state_transitions.json")


# -----------------------------------------------------------------------------
# Backward Compatibility
# -----------------------------------------------------------------------------

# Module-level constants loaded at import time for convenience
# These mirror the old inline definitions

try:
    AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = load_agent_tools()
    MANAGER_TOOLS: list[dict[str, Any]] = load_manager_tools()
    MANAGER_TOOL_CHOICE: dict[str, Any] = load_manager_tool_choice()
    VALIDATION_RULES: dict[str, Any] = load_validation_rules()
    STATE_TRANSITIONS: dict[str, Any] = load_state_transitions()
except Exception as e:
    logger.warning(f"Failed to preload schemas: {e}. Will retry on demand.")
    AGENT_TOOL_SCHEMAS = []
    MANAGER_TOOLS = []
    MANAGER_TOOL_CHOICE = {}
    VALIDATION_RULES = {}
    STATE_TRANSITIONS = {}


__all__ = [
    # Loader functions
    "load_agent_tools",
    "load_manager_tools",
    "load_manager_tool_choice",
    "load_validation_rules",
    "load_state_transitions",
    # Pre-loaded constants (backward compatibility)
    "AGENT_TOOL_SCHEMAS",
    "MANAGER_TOOLS",
    "MANAGER_TOOL_CHOICE",
    "VALIDATION_RULES",
    "STATE_TRANSITIONS",
]
