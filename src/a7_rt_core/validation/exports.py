"""
A7-RT Export Validator — Standalone Import Probe Utility

Validates that Python modules actually export what their interface declares.
Uses runtime import probe (not static analysis) to catch dynamic __all__ issues.

Scope: Python files only. Advisory tool for builders to catch export mismatches
before expensive test cycles.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class ExportValidationError(Exception):
    """Export validation-specific errors."""

    pass


def import_probe(
    code_path: Path | str,
    expected_exports: list[str],
    timeout: int = 10,
) -> dict[str, Any]:
    """
    Execute import in subprocess, check exports exist.

    Args:
        code_path: Path to Python file to validate
        expected_exports: List of export names that should exist
        timeout: Maximum seconds to wait for import

    Returns:
        {
            "passed": bool,
            "missing": list[str],      # Names not found
            "found": list[str],        # Names that exist
            "error": str | None,       # Import/probe error message
        }
    """
    code_path = Path(code_path)
    module_dir = str(code_path.parent)
    module_name = code_path.stem

    # Build probe script that imports and checks exports
    test_script = f"""
import sys
sys.path.insert(0, {module_dir!r})
import json

try:
    mod = __import__({module_name!r})
    # Combine dir() and __all__ to find available exports
    available = set(dir(mod))
    if hasattr(mod, "__all__"):
        available.update(getattr(mod, "__all__"))

    result = {{}}
    for name in {expected_exports!r}:
        result[name] = name in available

    print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"_error": str(e)}}))
"""

    try:
        proc = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "missing": expected_exports,
            "found": [],
            "error": f"Import probe timed out after {timeout}s",
        }

    # Parse probe output
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "passed": False,
            "missing": expected_exports,
            "found": [],
            "error": f"Probe output parse failed: {proc.stdout[:200]}",
        }

    # Handle import errors
    if "_error" in data:
        return {
            "passed": False,
            "missing": expected_exports,
            "found": [],
            "error": data["_error"],
        }

    # Determine missing vs found
    missing = [name for name, found in data.items() if not found]
    found = [name for name, found in data.items() if found]

    return {
        "passed": len(missing) == 0,
        "missing": missing,
        "found": found,
        "error": None,
    }


def validate_exports_in_directory(
    directory: Path | str,
    interface_exports: list[str],
    file_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """
    Validate exports against all matching Python files in directory.

    Args:
        directory: Root directory to search for Python files
        interface_exports: Expected exports from node interface
        file_patterns: Glob patterns to match (default: ["*.py", "**/*.py"])

    Returns:
        {
            "status": "exports_ok" | "exports_missing" | "no_python_files" | "error",
            "failures": list[{
                "file": str,
                "missing": list[str],
            }],
            "suggestion": str,
        }
    """
    directory = Path(directory)

    if not directory.exists():
        return {
            "status": "error",
            "failures": [],
            "suggestion": f"Directory not found: {directory}",
        }

    # Default patterns: all Python files
    if file_patterns is None:
        file_patterns = ["*.py", "**/*.py"]

    # Collect Python files
    python_files: set[Path] = set()
    for pattern in file_patterns:
        if pattern.startswith("**/"):
            # Recursive pattern
            python_files.update(directory.rglob(pattern[3:]))
        else:
            # Non-recursive pattern
            python_files.update(directory.glob(pattern))

    python_files = {f for f in python_files if f.is_file()}

    if not python_files:
        return {
            "status": "no_python_files",
            "failures": [],
            "suggestion": "No Python files found to validate",
        }

    # Validate against each Python file
    failures = []
    for py_file in sorted(python_files):
        result = import_probe(py_file, interface_exports)
        if not result["passed"]:
            failures.append(
                {
                    "file": str(py_file.relative_to(directory)),
                    "missing": result["missing"],
                }
            )

    if failures:
        return {
            "status": "exports_missing",
            "failures": failures,
            "suggestion": "Add missing names to __all__ or fix interface.exports",
        }

    return {
        "status": "exports_ok",
        "failures": [],
        "suggestion": "All declared exports found in module(s)",
    }


def validate_shadow_exports(
    shadow_writes: dict[str, str],
    interface_exports: list[str],
    temp_dir: Path | str,
) -> dict[str, Any]:
    """
    Validate exports for shadow-written Python files staged to temp directory.

    Args:
        shadow_writes: Dict of path -> content from ShadowFS.get_writes()
        interface_exports: Expected exports from node interface
        temp_dir: Staged temp directory from ShadowFS.stage_to_temp()

    Returns:
        {
            "status": "exports_ok" | "exports_missing" | "no_python_files",
            "failures": list[{
                "file": str,
                "missing": list[str],
            }],
            "suggestion": str,
        }
    """
    temp_dir = Path(temp_dir)

    # Filter to only Python files that were written
    python_files = [path for path in shadow_writes.keys() if path.endswith(".py")]

    if not python_files:
        return {
            "status": "no_python_files",
            "failures": [],
            "suggestion": "No Python files in shadow writes to validate",
        }

    # Validate each Python file in the staged directory
    failures = []
    for rel_path in python_files:
        full_path = temp_dir / rel_path
        if not full_path.exists():
            continue  # Shouldn't happen, but be defensive

        result = import_probe(full_path, interface_exports)
        if not result["passed"]:
            failures.append(
                {
                    "file": rel_path,
                    "missing": result["missing"],
                }
            )

    if failures:
        return {
            "status": "exports_missing",
            "failures": failures,
            "suggestion": "Add missing names to __all__ or fix interface.exports",
        }

    return {
        "status": "exports_ok",
        "failures": [],
        "suggestion": "All declared exports found in module(s)",
    }
