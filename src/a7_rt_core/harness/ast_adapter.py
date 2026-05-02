"""
AST Adapter — Universal language-aware symbol extraction.

Provides a common interface for extracting function/class signatures across
multiple languages. Python uses stdlib ast (zero dependencies). Other languages
use tree-sitter when available, falling back to regex.

Architecture:
- ExtractedSymbol: Common dataclass for all languages
- Language extractors: Implement extract() -> list[ExtractedSymbol]
- Unified entry point: extract_symbols(content, filename, filter_names)
"""

from __future__ import annotations

import abc
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from collections.abc import Iterator


# -----------------------------------------------------------------------------
# Common Schema
# -----------------------------------------------------------------------------


@dataclass
class ExtractedSymbol:
    """
    Common interface across all languages.

    Core fields (always populated):
        name: Symbol identifier
        kind: "function", "class", "interface", "type", "const", "import"
        line: 1-based line number in source
        signature: Human-readable signature string (language-native format)

    Optional extensions (populated per-language capabilities):
        type_annotation: Return type (Python, TypeScript, Go)
        is_async: Function is async/await (Python, JavaScript, Rust)
        is_exported: Exported from module (JavaScript/TypeScript)
        visibility: "public", "private", "pub", "export", "internal"
        receiver: Method receiver type (Go, Rust)
        decorators: Decorators/annotations (Python, TypeScript)
        generics: Generic type parameters (TypeScript, Rust, Go)
    """

    # Core (required)
    name: str
    kind: str
    line: int
    signature: str

    # Optional extensions
    type_annotation: Optional[str] = None
    is_async: bool = False
    is_exported: bool = True
    visibility: Optional[str] = None
    receiver: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    generics: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Abstract Base
# -----------------------------------------------------------------------------


class BaseExtractor(abc.ABC):
    """Abstract base for language-specific extractors."""

    @abc.abstractmethod
    def extract(
        self, content: str, filter_names: list[str] | None = None
    ) -> list[ExtractedSymbol]:
        """Extract symbols from content, optionally filtered by name."""
        pass


# -----------------------------------------------------------------------------
# Python Extractor (stdlib ast — zero dependencies)
# -----------------------------------------------------------------------------


class PythonExtractor(BaseExtractor):
    """
    Python extraction using stdlib ast module.

    Full support for: type annotations, async/await, decorators,
    argument defaults, *args, **kwargs.
    """

    def extract(
        self, content: str, filter_names: list[str] | None = None
    ) -> list[ExtractedSymbol]:
        import ast

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        symbols = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if filter_names and node.name not in filter_names:
                    continue

                sig = self._build_signature(node)
                symbols.append(
                    ExtractedSymbol(
                        name=node.name,
                        kind="function",
                        line=node.lineno,
                        signature=sig,
                        type_annotation=self._get_return_type(node),
                        is_async=isinstance(node, ast.AsyncFunctionDef),
                        visibility=self._get_visibility(node, tree),
                        decorators=[ast.unparse(d) for d in node.decorator_list],
                    )
                )

            elif isinstance(node, ast.ClassDef):
                if filter_names and node.name not in filter_names:
                    continue

                sig = self._build_class_signature(node)
                symbols.append(
                    ExtractedSymbol(
                        name=node.name,
                        kind="class",
                        line=node.lineno,
                        signature=sig,
                        visibility=self._get_visibility(node, tree),
                        decorators=[ast.unparse(d) for d in node.decorator_list],
                    )
                )

        # Sort by line number for stable ordering
        symbols.sort(key=lambda s: s.line)
        return symbols

    def _build_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """Reconstruct function signature from AST."""
        import ast

        async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        args_str = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"{async_prefix}def {node.name}({args_str}){returns}"

    def _build_class_signature(self, node: ast.ClassDef) -> str:
        """Reconstruct class signature with bases."""
        import ast

        if node.bases:
            bases_str = ", ".join(ast.unparse(base) for base in node.bases)
            return f"class {node.name}({bases_str}):"
        return f"class {node.name}:"

    def _get_return_type(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> Optional[str]:
        """Extract return type annotation if present."""
        import ast

        if node.returns:
            return ast.unparse(node.returns)
        return None

    def _get_visibility(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, tree: ast.AST
    ) -> str:
        """
        Determine visibility. Python default is public.
        Check for leading underscore (private convention) or __all__ membership.
        """
        if node.name.startswith("_") and not node.name.startswith("__"):
            return "private"
        # Could check __all__ here for module-level exports
        return "public"


# -----------------------------------------------------------------------------
# Tree-sitter Extractors (optional dependencies)
# -----------------------------------------------------------------------------


class TreeSitterExtractor(BaseExtractor):
    """
    Base class for tree-sitter based extractors.
    Handles common tree-sitter operations.
    """

    LANGUAGE = None  # Override in subclass
    LANGUAGE_NAME = "unknown"

    # Map tree-sitter node types to normalized kinds
    KIND_MAP: dict[str, str] = {}

    def __init__(self):
        self._parser = None
        self._language = None
        self._init_language()

    def _init_language(self) -> bool:
        """Initialize tree-sitter parser. Return success status."""
        try:
            from tree_sitter import Language, Parser

            if self.LANGUAGE is None:
                return False

            self._language = Language(self.LANGUAGE)
            self._parser = Parser(self._language)
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        """Check if this extractor can be used (dependencies available)."""
        return self._parser is not None

    def extract(
        self, content: str, filter_names: list[str] | None = None
    ) -> list[ExtractedSymbol]:
        if not self.is_available():
            return []

        try:
            tree = self._parser.parse(content.encode("utf-8"))
            symbols = list(
                self._walk_and_extract(tree.root_node, content, filter_names)
            )
            symbols.sort(key=lambda s: s.line)
            return symbols
        except Exception:
            return []

    def _walk_and_extract(
        self, node, content: str, filter_names: list[str] | None
    ) -> Iterator[ExtractedSymbol]:
        """Walk tree and yield ExtractedSymbol instances."""
        # Language-specific subclasses override this
        yield from self._traverse_node(node, content, filter_names)

    def _traverse_node(
        self, node, content: str, filter_names: list[str] | None
    ) -> Iterator[ExtractedSymbol]:
        """Recursive traversal — base implementation."""
        # Process current node
        symbol = self._process_node(node, content, filter_names)
        if symbol:
            yield symbol

        # Recurse into children
        for child in node.children:
            yield from self._traverse_node(child, content, filter_names)

    def _process_node(
        self, node, content: str, filter_names: list[str] | None
    ) -> Optional[ExtractedSymbol]:
        """Process a single node. Override in subclasses."""
        return None

    def _node_text(self, node, content: str) -> str:
        """Extract text for a node using tree-sitter's byte handling."""
        if node.text is not None:
            return (
                node.text.decode("utf-8") if isinstance(node.text, bytes) else node.text
            )
        return content[node.start_byte : node.end_byte]

    def _get_child_by_field(self, node, field_name: str):
        """Safely get child by field name."""
        try:
            for child in node.children:
                if child.type == field_name or (
                    hasattr(child, "field_name") and child.field_name == field_name
                ):
                    return child
        except Exception:
            pass
        return None


class TypeScriptExtractor(TreeSitterExtractor):
    """TypeScript/JavaScript extraction via tree-sitter."""

    LANGUAGE_NAME = "typescript"

    KIND_MAP = {
        "function_declaration": "function",
        "function_signature": "function",
        "method_signature": "function",
        "method_definition": "function",
        "arrow_function": "function",
        "function_expression": "function",
        "class_declaration": "class",
        "class_expression": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    }

    def __init__(self):
        self.LANGUAGE = self._try_load_language()
        super().__init__()

    def _try_load_language(self):
        try:
            import tree_sitter_typescript as tstypescript

            return tstypescript.language()
        except Exception:
            # Try JavaScript as fallback
            try:
                import tree_sitter_javascript as tsjs

                return tsjs.language()
            except Exception:
                return None

    def _process_node(
        self, node, content: str, filter_names: list[str] | None
    ) -> Optional[ExtractedSymbol]:
        node_type = node.type

        if node_type not in self.KIND_MAP:
            return None

        name = self._extract_name(node)
        if not name:
            return None

        if filter_names and name not in filter_names:
            return None

        kind = self.KIND_MAP[node_type]
        signature = self._reconstruct_signature(node, content, name, kind)

        return ExtractedSymbol(
            name=name,
            kind=kind,
            line=node.start_point[0] + 1,  # 0-indexed to 1-indexed
            signature=signature,
            is_async=self._is_async(node),
            is_exported=self._is_exported(node),
            visibility="export" if self._is_exported(node) else "internal",
            type_annotation=self._extract_return_type(node, content),
            generics=self._extract_generics(node, content),
        )

    def _extract_name(self, node) -> Optional[str]:
        """Extract identifier name from node."""
        for child in node.children:
            if child.type == "identifier":
                return (
                    child.text.decode("utf-8")
                    if isinstance(child.text, bytes)
                    else child.text
                )
            # TypeScript property_identifier for methods
            if child.type == "property_identifier":
                return (
                    child.text.decode("utf-8")
                    if isinstance(child.text, bytes)
                    else child.text
                )
        return None

    def _reconstruct_signature(self, node, content: str, name: str, kind: str) -> str:
        """Build human-readable signature."""
        if kind == "class":
            heritage = ""
            for child in node.children:
                if child.type == "class_heritage":
                    heritage = self._node_text(child, content).strip()
                    break
            return (
                f"class {name} {heritage}".strip() + " {"
                if heritage
                else f"class {name} {{"
            )

        elif kind == "interface":
            return f"interface {name} {{"

        elif kind == "type":
            return f"type {name} = ..."

        # Function — try to get parameters
        params = ""
        for child in node.children:
            if child.type in ("formal_parameters", "parameters"):
                params = self._node_text(child, content).strip()
                break

        async_prefix = "async " if self._is_async(node) else ""
        return_type = self._extract_return_type(node, content)
        return_suffix = f": {return_type}" if return_type else ""

        return f"{async_prefix}function {name}{params}{return_suffix}"

    def _is_async(self, node) -> bool:
        """Check if function has async modifier."""
        for child in node.children:
            if child.type == "async":
                return True
        return False

    def _is_exported(self, node) -> bool:
        """Check if node is exported."""
        # Walk up to check for export statement
        parent = node.parent
        if parent and parent.type == "export_statement":
            return True
        # Check for export keyword in children
        for child in node.children:
            if child.type == "export":
                return True
        return False

    def _extract_return_type(self, node, content: str) -> Optional[str]:
        """Extract TypeScript return type annotation."""
        for child in node.children:
            if child.type == "type_annotation":
                text = self._node_text(child, content).strip()
                return text.lstrip(":").strip()
            # For method signature in interface
            if child.type == "type_annotation":
                return self._node_text(child, content).strip().lstrip(":")
        return None

    def _extract_generics(self, node, content: str) -> list[str]:
        """Extract generic type parameters."""
        for child in node.children:
            if child.type == "type_parameters":
                text = self._node_text(child, content).strip()
                # Parse <T, U extends Foo> into list
                inner = text[1:-1] if text.startswith("<") else text
                return [p.strip() for p in inner.split(",")]
        return []


class GoExtractor(TreeSitterExtractor):
    """Go extraction via tree-sitter."""

    LANGUAGE_NAME = "go"

    KIND_MAP = {
        "function_declaration": "function",
        "method_declaration": "function",
        "type_declaration": "type",
    }

    def __init__(self):
        self.LANGUAGE = self._try_load_language()
        super().__init__()

    def _try_load_language(self):
        try:
            import tree_sitter_go as tsgo

            return tsgo.language()
        except Exception:
            return None

    def _process_node(
        self, node, content: str, filter_names: list[str] | None
    ) -> Optional[ExtractedSymbol]:
        node_type = node.type

        if node_type not in self.KIND_MAP:
            return None

        name = self._extract_name(node)
        if not name:
            return None

        if filter_names and name not in filter_names:
            return None

        kind = self.KIND_MAP[node_type]
        signature = self._reconstruct_signature(node, content, name, kind)

        # Go visibility: capitalized = public
        visibility = "public" if name[0].isupper() else "private"

        return ExtractedSymbol(
            name=name,
            kind=kind,
            line=node.start_point[0] + 1,
            signature=signature,
            visibility=visibility,
            receiver=self._extract_receiver(node, content),
        )

    def _extract_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "identifier":
                text = child.text
                return text.decode("utf-8") if isinstance(text, bytes) else text
            # For type declarations
            if child.type == "type_spec":
                for sub in child.children:
                    if sub.type == "type_identifier":
                        text = sub.text
                        return text.decode("utf-8") if isinstance(text, bytes) else text
        return None

    def _reconstruct_signature(self, node, content: str, name: str, kind: str) -> str:
        if kind == "type":
            # struct or interface
            for child in node.children:
                if child.type == "type_spec":
                    type_name = self._extract_name(child)
                    return f"type {type_name} ..."
            return f"type {name} ..."

        # Function or method
        receiver = self._extract_receiver(node, content)
        params = ""
        results = ""

        for child in node.children:
            if child.type == "parameter_list":
                params = self._node_text(child, content).strip()
            elif child.type == "result":
                results = self._node_text(child, content).strip()

        recv_prefix = f"{receiver} " if receiver else ""
        results_suffix = f" {results}" if results else ""
        return f"func {recv_prefix}{name}{params}{results_suffix}"

    def _extract_receiver(self, node, content: str) -> Optional[str]:
        """Extract method receiver: (t *Type)"""
        if node.type != "method_declaration":
            return None
        for child in node.children:
            if child.type == "receiver":
                return self._node_text(child, content).strip()
        return None


class RustExtractor(TreeSitterExtractor):
    """Rust extraction via tree-sitter."""

    LANGUAGE_NAME = "rust"

    KIND_MAP = {
        "function_item": "function",
        "impl_item": "impl",
        "struct_item": "class",
        "trait_item": "interface",
    }

    def __init__(self):
        self.LANGUAGE = self._try_load_language()
        super().__init__()

    def _try_load_language(self):
        try:
            import tree_sitter_rust as tsrust

            return tsrust.language()
        except Exception:
            return None

    def _process_node(
        self, node, content: str, filter_names: list[str] | None
    ) -> Optional[ExtractedSymbol]:
        node_type = node.type

        if node_type not in self.KIND_MAP:
            return None

        name = self._extract_name(node)
        if not name:
            return None

        if filter_names and name not in filter_names:
            return None

        kind = self.KIND_MAP[node_type]
        signature = self._reconstruct_signature(node, content, name, kind)

        # Rust visibility: pub, pub(crate), etc.
        visibility = self._extract_visibility(node)

        return ExtractedSymbol(
            name=name,
            kind=kind,
            line=node.start_point[0] + 1,
            signature=signature,
            visibility=visibility,
            is_async=self._is_async(node),
            generics=self._extract_generics(node, content),
        )

    def _extract_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "identifier":
                text = child.text
                return text.decode("utf-8") if isinstance(text, bytes) else text
        return None

    def _reconstruct_signature(self, node, content: str, name: str, kind: str) -> str:
        if kind == "impl":
            trait = ""
            for child in node.children:
                if child.type == "type_identifier":
                    trait = self._node_text(child, content).strip()
            return f"impl {trait} {{ ... }}" if trait else "impl { ... }"

        elif kind == "class":  # struct
            return f"struct {name} {{ ... }}"

        elif kind == "interface":  # trait
            return f"trait {name} {{ ... }}"

        # Function
        params = ""
        ret_type = ""

        for child in node.children:
            if child.type == "parameters":
                params = self._node_text(child, content).strip()
            elif child.type == "return_type":
                ret_type = self._node_text(child, content).strip()

        async_prefix = "async " if self._is_async(node) else ""
        ret_suffix = f" -> {ret_type}" if ret_type else ""
        return f"{async_prefix}fn {name}{params}{ret_suffix}"

    def _extract_visibility(self, node) -> str:
        for child in node.children:
            if child.type == "visibility_modifier":
                text = (
                    child.text.decode("utf-8")
                    if isinstance(child.text, bytes)
                    else child.text
                )
                return text.strip()  # pub, pub(crate), etc.
        return "private"

    def _is_async(self, node) -> bool:
        for child in node.children:
            if child.type == "async":
                return True
        return False

    def _extract_generics(self, node, content: str) -> list[str]:
        for child in node.children:
            if child.type == "generic_type":
                text = self._node_text(child, content).strip()
                return [text]  # Simplified
            if child.type == "type_parameters":
                text = self._node_text(child, content).strip()
                inner = text[1:-1] if text.startswith("<") else text
                return [p.strip() for p in inner.split(",")]
        return []


# -----------------------------------------------------------------------------
# Fallback Regex Extractor
# -----------------------------------------------------------------------------


class RegexExtractor(BaseExtractor):
    """
    Generic regex-based extraction for unsupported languages.
    Lower quality but always available.
    """

    PATTERNS = [
        # Python-style
        re.compile(
            r"^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^:]+))?:",
            re.MULTILINE,
        ),
        # Class definitions
        re.compile(
            r"^(?:export\s+)?(?:class|struct)\s+(\w+)(?:\s*[:{])?", re.MULTILINE
        ),
        # JavaScript/TypeScript function
        re.compile(
            r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)",
            re.MULTILINE,
        ),
        # Go function
        re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)", re.MULTILINE),
        # Rust function
        re.compile(
            r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*<[^>]*>\s*\(([^)]*)\)|^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(([^)]*)\)",
            re.MULTILINE,
        ),
    ]

    def extract(
        self, content: str, filter_names: list[str] | None = None
    ) -> list[ExtractedSymbol]:
        symbols = []
        seen = set()

        for pattern in self.PATTERNS:
            for match in pattern.finditer(content):
                name = match.group(1)
                if not name or name in seen:
                    continue
                if filter_names and name not in filter_names:
                    continue

                seen.add(name)
                line = content[: match.start()].count("\n") + 1

                # Determine kind
                kind = "function"
                if "class" in match.group(0) or "struct" in match.group(0):
                    kind = "class"

                # Extract params if available
                params = ""
                if len(match.groups()) >= 2 and match.group(2):
                    params = f"({match.group(2)})"
                else:
                    params = "()"

                # Return type if available
                return_type = ""
                if len(match.groups()) >= 3 and match.group(3):
                    return_type = f" -> {match.group(3).strip()}"

                signature = f"def {name}{params}{return_type}"
                if kind == "class":
                    signature = f"class {name}:"

                symbols.append(
                    ExtractedSymbol(
                        name=name,
                        kind=kind,
                        line=line,
                        signature=signature,
                    )
                )

        symbols.sort(key=lambda s: s.line)
        return symbols


# -----------------------------------------------------------------------------
# Unified Entry Point
# -----------------------------------------------------------------------------


class MarkdownExtractor(BaseExtractor):
    """Markdown header extraction."""

    def extract(
        self, content: str, filter_names: list[str] | None = None
    ) -> list[ExtractedSymbol]:
        """Extract H1-H3 headers as symbols."""
        import re

        header_pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
        symbols = []

        for match in header_pattern.finditer(content):
            level = len(match.group(1))
            text = match.group(2).strip()
            line = content[: match.start()].count("\n") + 1

            # Skip if filtered
            if filter_names and text not in filter_names:
                continue

            symbols.append(
                ExtractedSymbol(
                    name=text,
                    kind="header",
                    line=line,
                    signature=f"{'#' * level} {text}",
                )
            )

        return symbols


def _build_extractor_registry() -> dict[str, BaseExtractor]:
    """Build registry of available extractors."""
    registry: dict[str, BaseExtractor] = {
        "py": PythonExtractor(),
        "md": MarkdownExtractor(),
        "markdown": MarkdownExtractor(),
    }

    # Try to add tree-sitter extractors
    ts_js = TypeScriptExtractor()
    if ts_js.is_available():
        registry["js"] = ts_js
        registry["ts"] = ts_js
        registry["tsx"] = ts_js

    go = GoExtractor()
    if go.is_available():
        registry["go"] = go

    rust = RustExtractor()
    if rust.is_available():
        registry["rs"] = rust

    return registry


# Global registry, built once
_EXTRACTOR_REGISTRY = _build_extractor_registry()
_REGEX_FALLBACK = RegexExtractor()


def extract_symbols(
    content: str,
    filename: str,
    filter_names: list[str] | None = None,
) -> tuple[list[ExtractedSymbol], str]:
    """
    Universal symbol extraction.

    Args:
        content: Source code as string
        filename: Original filename (used to determine language)
        filter_names: Optional list of names to filter for

    Returns:
        Tuple of (symbols list, source identifier)
        Source is the extractor used: "py", "ts", "go", "regex", etc.
    """
    ext = Path(filename).suffix.lstrip(".").lower() if "." in filename else ""

    # Try registered extractor
    extractor = _EXTRACTOR_REGISTRY.get(ext)
    if extractor:
        try:
            symbols = extractor.extract(content, filter_names)
            if symbols:  # Only use if we got results
                return symbols, ext
        except Exception:
            pass  # Fall through to regex

    # Fallback to regex
    return _REGEX_FALLBACK.extract(content, filter_names), "regex"


# -----------------------------------------------------------------------------
# Output Normalizers (for metadata fields)
# -----------------------------------------------------------------------------


def build_plumbing_summary(symbols: list[ExtractedSymbol]) -> str:
    """
    Build plumbing_summary string from extracted symbols.

    Format: "Exports: func1, func2, class1 | Imports: module1"
    """
    if not symbols:
        return "No exports detected"

    exports = [s.name for s in symbols if s.kind in ("function", "class", "interface")]
    imports = [s.name for s in symbols if s.kind == "import"]

    parts = []
    if exports:
        display = exports[:3]
        suffix = "..." if len(exports) > 3 else ""
        parts.append(f"Exports: {', '.join(display)}{suffix}")

    if imports:
        display = imports[:2]
        suffix = "..." if len(imports) > 2 else ""
        parts.append(f"Imports: {', '.join(display)}{suffix}")

    return " | ".join(parts) if parts else "No exports detected"


def get_first_export_preview(symbols: list[ExtractedSymbol]) -> str:
    """Get first function/class signature for preview."""
    for s in symbols:
        if s.kind in ("function", "class", "interface", "type"):
            return s.signature[:120]
    return ""


def get_export_signatures(
    symbols: list[ExtractedSymbol], filter_names: list[str] | None = None
) -> list[str]:
    """
    Get signatures, optionally filtered to specific names.

    This is the primary entry for SEAL enrichment.
    """
    if filter_names:
        return [s.signature for s in symbols if s.name in filter_names]
    return [
        s.signature for s in symbols if s.kind in ("function", "class", "interface")
    ]


# -----------------------------------------------------------------------------
# Compatibility: Old-style extraction for gradual migration
# -----------------------------------------------------------------------------


def extract_metadata_fields(content: str, filename: str) -> tuple[str, str, list[str]]:
    """
    Extract the three fields needed for NodeMetadata.

    Returns: (plumbing_summary, first_export_preview, all_signatures)
    """
    symbols, source = extract_symbols(content, filename)
    return (
        build_plumbing_summary(symbols),
        get_first_export_preview(symbols),
        [s.signature for s in symbols],
    )
