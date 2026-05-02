"""
Common utilities shared across harness mixins.

This module provides shared helper functions to avoid duplication
across harness mixin classes. All functions are stateless and
side-effect free unless explicitly documented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    """
    Return ISO format UTC timestamp.

    Returns:
        ISO 8601 formatted timestamp in UTC (e.g., "2024-01-15T10:30:00+00:00")
    """
    return datetime.now(timezone.utc).isoformat()


def _update_state(state: Any, **kwargs: Any) -> Any:
    """
    Return a new state object with updated fields, re-validating invariants.

    Uses Pydantic's model_dump() and model_validate() to ensure
    all validators run on the updated state.

    Args:
        state: Pydantic model instance (typically ManagerState)
        **kwargs: Fields to update

    Returns:
        New validated instance of the same type as state
    """
    data = state.model_dump(mode="json")
    data.update(kwargs)
    return type(state).model_validate(data)
