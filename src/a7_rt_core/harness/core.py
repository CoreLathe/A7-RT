"""
Harness core — combines all mixins into the main Harness class.

This file contains the assembled Harness class and any remaining
methods that don't fit cleanly into the mixin categories.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

# Import all mixins
from a7_rt_core.harness.apply import ApplyMixin
from a7_rt_core.harness.commit import CommitMixin
from a7_rt_core.harness.control import (
    ConsultHook,
    ControlMixin,
    HaltSignal,
    ManagerHook,
    SubagentHook,
)
from a7_rt_core.harness.dispatch import DispatchMixin
from a7_rt_core.harness.handlers import HandlersMixin
from a7_rt_core.harness.persistence import PersistenceMixin
from a7_rt_core.harness.utils import UtilsMixin, _now, _update_state
from a7_rt_core.harness.validate import ValidateMixin

# Re-export main types for backward compatibility
__all__ = [
    "Harness",
    "HaltSignal",
    "ManagerHook",
    "SubagentHook",
    "ConsultHook",
    "_now",
    "_update_state",
]


class Harness(
    ControlMixin,
    PersistenceMixin,
    ApplyMixin,
    DispatchMixin,
    ValidateMixin,
    CommitMixin,
    UtilsMixin,
    HandlersMixin,
):
    """
    A7-RT Harness — session lifecycle manager.

    Orchestrates the manager→agent→disk flow with transactional integrity
    via ShadowFS. All disk mutations go through this class (single writer).

    Mixins provide functionality by domain:
    - ControlMixin: __init__, step(), init_session(), lifecycle, routing
    - DispatchMixin: _handle_dispatch(), _handle_redispatch(), auto-gates
    - ValidateMixin: _handle_validate(), schema checking
    - CommitMixin: _handle_commit(), git integration
    - ApplyMixin: _apply_return(), _apply_edits(), file writes
    - PersistenceMixin: _checkpoint(), _drain_events(), resume
    - UtilsMixin: _validate_conventions(), metadata helpers
    - HandlersMixin: _handle_consult(), _handle_suspend(), _handle_seal(), _handle_update_plan()
    """

    def __init__(
        self,
        repo,
        manager_hook: ManagerHook,
        subagent_hook: SubagentHook,
        consult_hook: Optional[ConsultHook] = None,
        git_root: Optional[Path] = None,
        manager_budget: int = 8_000,
    ) -> None:
        """
        Initialize harness with repository and hook functions.

        Parameters
        ----------
        repo : Repository
            Open repository instance
        manager_hook : callable(board_view, state) -> ManagerAction
            Called each turn to get the manager's action
        subagent_hook : callable(node_id, role, context_view) -> SubagentReturn
            Called to dispatch work to a subagent
        consult_hook : optional callable
            Called for CONSULT actions (A7 engine integration)
        git_root : optional Path
            If set, run `git commit` after grounded nodes
        manager_budget : int
            Token limit for manager view; overflow -> HaltSignal
        """
        # Call ControlMixin's __init__ which sets up all the state
        super().__init__(
            repo=repo,
            manager_hook=manager_hook,
            subagent_hook=subagent_hook,
            consult_hook=consult_hook,
            git_root=git_root,
            manager_budget=manager_budget,
        )
