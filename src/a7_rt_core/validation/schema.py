"""
A7-RT Validator

Two responsibilities:
  1. Schema validation (structural): parse subagent return, check required fields,
     verify status is a valid NodeStatus value, check suspension_reason present
     when status=suspended, check interface has exports when status=grounded.
     Performed by the manager (via harness) before any state mutation.

  2. Hard-path validation (execution): run the test-author's test script in a
     sandboxed subprocess with a timeout. Pass → grounded. Fail → poisoned with
     named reason. Performed by the harness after schema validation.

Invariant: grounded status requires run_hard_path to return True.
No node achieves grounded without a passing test. This is enforced here, not by
convention.

Clean subprocess environment: the child inherits only PATH and PYTHONPATH (if set)
so that test outcomes are not masked by environment variables from the parent
process (credentials, debug flags, etc.).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Optional

from a7_rt_core.core.models import NodeStatus, SubagentReturn, SuspensionType

# ---------------------------------------------------------------------------
# Required fields per role - loaded from external schema for easy editing
#
# _REQUIRED_FIELDS: must be present (key in dict).
# _NONNULL_FIELDS:  must be present AND non-None.
#
# Distinction matters: a suspended builder legitimately has content=null,
# so 'content' is required-present but not required-non-null for builder.
#
# Source: a7_rt_core/schemas/harness/validation.json
# ---------------------------------------------------------------------------
from a7_rt_core.schemas import load_validation_rules

_validation_rules = load_validation_rules()

_REQUIRED_FIELDS: dict[str, list[str]] = {
    role: data["fields"] for role, data in _validation_rules.get("required_fields", {}).items()
}

_NONNULL_FIELDS: dict[str, list[str]] = {
    role: data["fields"] for role, data in _validation_rules.get("nonnull_fields", {}).items()
}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class Validator:
    """
    Schema check and hard-path test execution.

    Schema validation is stateless — call validate_schema with the raw parsed
    dict from the subagent and the role name.

    Hard-path execution is stateless — call run_hard_path with the test script
    string and an optional timeout in seconds.
    """

    # -----------------------------------------------------------------------
    # Schema validation
    # -----------------------------------------------------------------------

    def validate_schema(self, role: str, raw_return: dict) -> tuple[bool, str]:
        """
        Structurally validate the parsed return dict for the given role.

        Returns (valid: bool, error_detail: str).
        On success: (True, "").
        On failure: (False, human-readable reason).

        Checks performed:
          - Role is a known role
          - Required fields are present and non-None
          - status is a valid NodeStatus value (builder only)
          - suspension_reason present if status=suspended (builder only)
          - interface.exports is a non-empty list if status=grounded (builder only)
        """
        if role not in _REQUIRED_FIELDS:
            return (
                False,
                f"Unknown role: {role!r}. Expected one of: {list(_REQUIRED_FIELDS)}",
            )

        # Check required fields are present
        for field in _REQUIRED_FIELDS[role]:
            if field not in raw_return:
                return False, f"Missing required field: {field!r}"

        # Check non-null fields are not None
        for field in _NONNULL_FIELDS.get(role, []):
            if raw_return.get(field) is None:
                return False, f"Required field {field!r} is null"

        # Builder-specific checks
        if role == "builder":
            ok, err = self._validate_builder(raw_return)
            if not ok:
                return False, err

        return True, ""

    def validate_return(self, result: SubagentReturn, node_id: str) -> tuple[bool, str]:
        """
        Validate a parsed SubagentReturn against the schema for its role.

        Converts the typed SubagentReturn to a raw dict that matches the shape
        validate_schema expects, then delegates to validate_schema.

        node_id is required because SubagentReturn does not store it (the harness
        tracks it separately as part of the action).

        Returns (valid: bool, error_detail: str).
        """
        role = result.role
        sr = result.suspension_reason
        # Support both legacy content field and new files dict (multi-file PR)
        has_content = result.content is not None or bool(result.files)
        raw: dict = {
            "node_id": node_id,
            "status": result.status.value if result.status is not None else None,
            "content": result.content
            if result.content
            else ("<multifile>" if result.files else None),
            "interface": result.interface_update or {},
            "suspension_reason": sr.model_dump() if sr is not None else None,
            # test_author shape (test_script maps to content)
            "test_script": result.content,
            # analyst shape (query/verdict not stored in SubagentReturn; fill with sentinel)
            "query": node_id,
            "verdict": result.content or "(no verdict)",
        }
        return self.validate_schema(role, raw)

    def _validate_builder(self, data: dict) -> tuple[bool, str]:
        """Builder-specific schema checks."""
        # status must be a valid NodeStatus
        status_raw = data.get("status")
        try:
            status = NodeStatus(status_raw)
        except ValueError:
            valid_values = [s.value for s in NodeStatus]
            return False, (f"Invalid status: {status_raw!r}. Expected one of: {valid_values}")

        # suspension_reason required when suspended
        if status == NodeStatus.SUSPENDED:
            sr = data.get("suspension_reason")
            if not sr:
                return (
                    False,
                    "status=suspended requires suspension_reason to be present",
                )
            if not isinstance(sr, dict):
                return (
                    False,
                    f"suspension_reason must be a dict, got {type(sr).__name__}",
                )
            if "type" not in sr:
                return False, "suspension_reason missing 'type' field"
            valid_types = [t.value for t in SuspensionType]
            if sr["type"] not in valid_types:
                return False, (
                    f"suspension_reason.type {sr['type']!r} invalid. Expected one of: {valid_types}"
                )
            if "detail" not in sr or not sr["detail"]:
                return False, "suspension_reason missing non-empty 'detail' field"

        # grounded requires content (or files) and interface.exports
        if status == NodeStatus.GROUNDED:
            has_content = bool(data.get("content"))
            if not has_content:
                return (
                    False,
                    "status=grounded requires non-empty 'content' or 'files' field",
                )
            iface = data.get("interface")
            if not iface:
                return False, "status=grounded requires 'interface' field"
            if not isinstance(iface, dict):
                return False, f"'interface' must be a dict, got {type(iface).__name__}"
            exports = iface.get("exports")
            if not exports or not isinstance(exports, list):
                return False, (
                    "status=grounded requires interface.exports to be a non-empty list "
                    "(the node must declare what it exports)"
                )

        return True, ""

    # -----------------------------------------------------------------------
    # Hard-path validation
    # -----------------------------------------------------------------------

    def run_hard_path(
        self,
        test_script: str,
        timeout: int = 30,
        content_dir: "Optional[str]" = None,
        test_path: "Optional[str]" = None,
    ) -> tuple[bool, str]:
        """
        Run test_script in a sandboxed subprocess with a timeout.

        Returns (passed: bool, detail: str).
          - Pass:    (True, "PASS")
          - Fail:    (False, "<stdout+stderr with exit code>")
          - Timeout: (False, "timeout after <N>s")
          - Syntax error or crash is captured in the fail detail.

        The subprocess runs with a clean environment — only PATH and PYTHONPATH
        (if present) are inherited. No credentials, debug flags, or parent-process
        environment variables that could mask test failures.

        content_dir: if provided, prepended to PYTHONPATH so test scripts can
        import node implementations by node_id (e.g. `from node_a import add`).

        test_path: if provided, runs test from this file path instead of creating
        a temp file. This preserves __file__ for relative imports in tests.
        """
        # Use original test file if path provided, otherwise create temp file
        if test_path is not None:
            script_path = test_path
            # Write test script to the original file (for cases where it was staged in shadow)
            with open(test_path, "w", encoding="utf-8") as f:
                f.write(test_script)
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                prefix="a7rt_test_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(test_script)
                script_path = f.name

        try:
            env = _clean_env()
            if content_dir is not None:
                existing = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = content_dir if not existing else f"{content_dir}:{existing}"
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=content_dir,
            )
            if result.returncode == 0:
                return True, "PASS"
            else:
                combined = _combine_output(result.stdout, result.stderr)
                return False, (f"exit code {result.returncode}\n{combined}".strip())
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
        finally:
            # Only delete if we created a temp file
            if test_path is None:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clean_env() -> dict[str, str]:
    """
    Build a minimal environment for the test subprocess.

    Inherits only PATH (required to find interpreters) and PYTHONPATH (if set,
    so tests can import project modules). Everything else is stripped.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "PYTHONPATH"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def _combine_output(stdout: str, stderr: str) -> str:
    """Combine stdout and stderr into a single string, labelled if both present."""
    parts = []
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.rstrip()}")
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.rstrip()}")
    return "\n".join(parts)
