"""
Harness package for A7-RT.

This package contains the Harness orchestration logic split into mixins
for maintainability. The main entry point is the Harness class re-exported
from harness.core.

See harness.py at project root for architectural overview.
"""

# Main class and exception
# Hook type aliases
from a7_rt_core.harness.control import ConsultHook, ManagerHook, SubagentHook
from a7_rt_core.harness.core import HaltSignal, Harness

# Utility functions (for advanced usage)
from a7_rt_core.harness.utils import _compute_metadata, _now, _update_state

__all__ = [
    "Harness",
    "HaltSignal",
    "ManagerHook",
    "SubagentHook",
    "ConsultHook",
    "_now",
    "_update_state",
    "_compute_metadata",
]
