"""
UtilsMixin — Metadata extraction, convention validation, and helpers.

Refactored to use harness/ast_adapter for universal language support.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from a7_rt_core.core.models import FileTag, NodeMetadata, NodeStatus
from a7_rt_core.harness._common import _now, _update_state
from a7_rt_core.harness.ast_adapter import (
    extract_metadata_fields,
    extract_symbols,
    get_export_signatures,
)

if TYPE_CHECKING:
    from harness.control import ControlMixin


def _normalize_tag_content(content: str) -> str:
    """Normalize tag content for deduplication: lowercase, strip punctuation/spaces."""
    return re.sub(r"[^\w\s]", "", content.lower().strip())


def _merge_file_tags(
    repo,
    node_id: str,
    incoming_tags: list[dict],
    author_role: str,
    turn: int,
) -> None:
    """
    Merge incoming file_tags with existing node.tags.

    Validation:
    - Content length ≤ 200 chars (enforced by parser, double-checked here)
    - node_id and content must be present

    Deduplication:
    - Same node_id + normalized content = same tag
    - Merge: append author_role to confirmed_by if not present
    - Increment confidence (len(confirmed_by))

    New tags get:
    - Fresh uuid tag_id
    - author_turn set to current turn
    - confirmed_by = [author_role]
    """
    doc = repo._load()
    node = doc["nodes"].get(node_id)
    if not node:
        return  # Silently skip if node missing (shouldn't happen)

    # Load existing tags
    existing_tags: list[dict] = list(node.get("tags") or [])

    # Build lookup: normalized content -> existing tag index
    content_to_index: dict[str, int] = {}
    for i, tag in enumerate(existing_tags):
        norm = _normalize_tag_content(tag.get("content", ""))
        if norm:
            content_to_index[norm] = i

    for tag_dict in incoming_tags:
        content = tag_dict.get("content", "")
        tag_node_id = tag_dict.get("node_id", "")

        # Validation: skip invalid tags
        if not content or not tag_node_id:
            continue
        if len(content) > 200:
            content = content[:200]  # Truncate if somehow too long

        norm = _normalize_tag_content(content)

        if norm in content_to_index:
            # Merge with existing tag
            idx = content_to_index[norm]
            existing = existing_tags[idx]
            confirmed_by = list(existing.get("confirmed_by", []))
            if author_role not in confirmed_by:
                confirmed_by.append(author_role)
                existing["confirmed_by"] = confirmed_by
        else:
            # Create new tag
            # Validate category, default to quirk if invalid
            category = tag_dict.get("category", "quirk")
            valid_categories = {"quirk", "order", "scope", "perf", "warning"}
            if category not in valid_categories:
                category = "quirk"
            new_tag = FileTag(
                node_id=tag_node_id,
                author_role=author_role,
                author_turn=turn,
                category=category,
                content=content,
                propagate=tag_dict.get("propagate", True),
                confirmed_by=[author_role],
            )
            existing_tags.append(new_tag.model_dump(mode="json"))
            content_to_index[norm] = len(existing_tags) - 1

    # Persist merged tags
    repo.update_node(node_id, tags=existing_tags)


def _compute_metadata(content: str, filename: str) -> NodeMetadata:
    """
    Compute harness-side NodeMetadata from raw content string.

    tokens            — word count (len(content.split()))
    lines             — newline count + 1
    content_hash      — SHA-256 hex digest, first 16 chars
    first_export_preview — first exported function/class signature (single line)
    plumbing_summary  — 1-2 line structural summary

    Uses harness/ast_adapter for universal language support:
    - Python: stdlib ast (zero dependencies)
    - Other languages: tree-sitter when available, regex fallback
    """
    tokens = len(content.split())
    lines = content.count("\n") + 1
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # Use ast_adapter for extraction (universal language support)
    plumbing_summary, first_export, _ = extract_metadata_fields(content, filename)

    return NodeMetadata(
        tokens=tokens,
        lines=lines,
        content_hash=content_hash,
        first_export_preview=first_export,
        plumbing_summary=plumbing_summary,
    )


class UtilsMixin:
    """
    Convention validation and metadata utilities.
    """

    def _validate_conventions(
        self: "ControlMixin",
        node_id: str,
        files_to_write: dict[str, str],
        state: Any,
    ) -> list[dict]:
        """
        Check file writes against project conventions.
        Emits convention_violation events for violations, returns violations list.

        Conventions:
        - utils/ directory: utility functions only, no business logic
        """
        violations = []

        for rel_path in files_to_write.keys():
            # Convention: utils/ should not contain business logic
            if rel_path.startswith("utils/") and not self._is_utility_file(rel_path):
                violation = {
                    "file": rel_path,
                    "rule": "utils_purity",
                    "detail": f"File {rel_path} in utils/ may contain business logic. utils/ is for pure utilities only.",
                }
                violations.append(violation)
                self.repo.append_event(
                    {
                        "turn": state.turn,
                        "timestamp": _now(),
                        "actor": "harness",
                        "action": "convention_violation",
                        "target": node_id,
                        "detail": violation["detail"],
                    }
                )

        return violations

    def _is_utility_file(self: "ControlMixin", rel_path: str) -> bool:
        """Heuristic: check if file appears to be a utility based on naming."""
        utility_patterns = [
            r"helper",
            r"util",
            r"common",
            r"constants",
            r"config",
            r"logging",
            r"parser",
            r"formatter",
            r"validator",
            r"encoder",
            r"decoder",
        ]
        lower_path = rel_path.lower()
        filename = lower_path.split("/")[-1]
        return any(re.search(p, filename) for p in utility_patterns)


# -----------------------------------------------------------------------------
# Signature extraction (module-level, used by handlers.py for SEAL)
# -----------------------------------------------------------------------------


def _extract_signatures(
    content: str,
    filename: str,
    filter_names: list[str] | None = None,
) -> list[str]:
    """
    Extract function/class signatures from code for SEAL export_signatures.

    Uses harness/ast_adapter for universal language support:
    - Python: full AST with type annotations
    - TypeScript/JavaScript: tree-sitter with generics, async, visibility
    - Go: tree-sitter with receiver types
    - Rust: tree-sitter with traits, visibility
    - Markdown: H1-H3 headers
    - Others: regex fallback
    """
    ext = filename.split(".")[-1].lower() if "." in filename else ""

    # Markdown: extract headers as signatures
    if ext in ("md", "markdown"):
        return _extract_markdown_signatures(content)

    # Use ast_adapter for all code files
    symbols, _ = extract_symbols(content, filename, filter_names)
    return get_export_signatures(symbols, filter_names)


def _extract_markdown_signatures(content: str) -> list[str]:
    """Extract H1-H3 headers from markdown."""
    header_pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    signatures: list[str] = []

    for match in header_pattern.finditer(content):
        level = len(match.group(1))
        text = match.group(2).strip()
        signatures.append(f"{'#' * level} {text}")

    return signatures
