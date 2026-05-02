"""Data access utilities for bundled package data.

Provides reliable access to roles/, protocols/, and config files
regardless of installation type (editable or normal).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from os import PathLike

if sys.version_info >= (3, 12):
    from importlib.resources import files
else:
    from importlib_resources import files


def get_data_path() -> Path:
    """Return the path to the data directory.

    Uses importlib.resources for reliable access in both editable
    and normal installations.
    """
    return Path(files("a7_rt_core.data").__str__())


def get_roles_dir() -> Path:
    """Return the path to the roles/ directory containing prompt files."""
    return get_data_path() / "roles"


def get_protocols_dir() -> Path:
    """Return the path to the protocols/ directory containing protocol definitions."""
    return get_data_path() / "protocols"


def get_config_example() -> Path:
    """Return the path to config.toml.example."""
    return get_data_path() / "config.toml.example"


def read_role_prompt(role: str) -> str | None:
    """Read a role prompt file by name.

    Args:
        role: Role name (e.g., "builder", "test_author", "analyst", "manager_prompt")

    Returns:
        File contents as string, or None if file doesn't exist
    """
    path = get_roles_dir() / f"{role}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def read_protocol(name: str) -> str | None:
    """Read a protocol file by name.

    Args:
        name: Protocol name (e.g., "none", "lean", "full")

    Returns:
        File contents as string, or None if file doesn't exist
    """
    path = get_protocols_dir() / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None
