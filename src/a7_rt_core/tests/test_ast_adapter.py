"""
Tests for harness/ast_adapter.py — Universal AST extraction.

Covers:
- Python extraction (stdlib ast)
- Filter names functionality
- Markdown header extraction
- Regex fallback for unsupported languages
- Metadata field assembly
- Edge cases (empty content, syntax errors)
"""

from __future__ import annotations

import pytest

from a7_rt_core.harness.ast_adapter import (
    ExtractedSymbol,
    PythonExtractor,
    RegexExtractor,
    build_plumbing_summary,
    extract_metadata_fields,
    extract_symbols,
    get_export_signatures,
    get_first_export_preview,
)


class TestPythonExtractor:
    """Tests for Python stdlib ast-based extraction."""

    def test_extract_function(self):
        code = """
def hello(name: str) -> str:
    return f"Hello, {name}"
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert len(symbols) == 1
        assert symbols[0].name == "hello"
        assert symbols[0].kind == "function"
        assert symbols[0].signature == "def hello(name: str) -> str"
        assert symbols[0].type_annotation == "str"
        assert symbols[0].is_async is False

    def test_extract_async_function(self):
        code = """
async def fetch(url: str) -> dict:
    return {}
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert len(symbols) == 1
        assert symbols[0].name == "fetch"
        assert symbols[0].is_async is True
        assert "async def fetch" in symbols[0].signature

    def test_extract_class(self):
        code = """
class MyClass(BaseClass):
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert len(symbols) == 1
        assert symbols[0].name == "MyClass"
        assert symbols[0].kind == "class"
        assert "MyClass(BaseClass)" in symbols[0].signature

    def test_extract_class_no_bases(self):
        code = """
class Simple:
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert symbols[0].signature == "class Simple:"

    def test_extract_with_decorators(self):
        code = """
@property
def my_prop(self) -> int:
    return 0
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert symbols[0].name == "my_prop"
        assert "property" in symbols[0].decorators

    def test_extract_multiple_symbols(self):
        code = """
def func1():
    pass

class Class1:
    pass

async def func2():
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert len(symbols) == 3
        assert symbols[0].name == "func1"
        assert symbols[1].name == "Class1"
        assert symbols[2].name == "func2"

    def test_filter_names(self):
        code = """
def keep():
    pass

def ignore():
    pass

class KeepClass:
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code, filter_names=["keep", "KeepClass"])

        assert len(symbols) == 2
        assert symbols[0].name == "keep"
        assert symbols[1].name == "KeepClass"

    def test_filter_names_not_found(self):
        code = """
def existing():
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code, filter_names=["nonexistent"])

        assert len(symbols) == 0

    def test_syntax_error_returns_empty(self):
        code = "def broken(:"  # Invalid syntax
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert symbols == []

    def test_line_numbers(self):
        code = """# Line 1
# Line 2
def on_line_3():
    pass

# Line 6
class OnLine7:
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        assert symbols[0].line == 3
        assert symbols[1].line == 7

    def test_visibility_private(self):
        code = """
def _private():
    pass

def public():
    pass
"""
        extractor = PythonExtractor()
        symbols = extractor.extract(code)

        private = next(s for s in symbols if s.name == "_private")
        public = next(s for s in symbols if s.name == "public")

        assert private.visibility == "private"
        assert public.visibility == "public"


class TestRegexExtractor:
    """Tests for regex-based fallback extraction."""

    def test_extract_python_style_function(self):
        code = """
def helper(x, y):
    return x + y
"""
        extractor = RegexExtractor()
        symbols = extractor.extract(code)

        assert len(symbols) >= 1
        assert any(s.name == "helper" for s in symbols)

    def test_extract_class(self):
        code = """
class MyClass:
    pass
"""
        extractor = RegexExtractor()
        symbols = extractor.extract(code)

        assert any(s.name == "MyClass" and s.kind == "class" for s in symbols)

    def test_extract_javascript_function(self):
        code = """
function doSomething(arg) {
    return arg;
}
"""
        extractor = RegexExtractor()
        symbols = extractor.extract(code)

        assert any(s.name == "doSomething" for s in symbols)

    def test_extract_go_function(self):
        code = """
func Process(data string) error {
    return nil
}
"""
        extractor = RegexExtractor()
        symbols = extractor.extract(code)

        assert any(s.name == "Process" for s in symbols)

    def test_filter_names(self):
        code = """
def keep():
    pass

def ignore():
    pass
"""
        extractor = RegexExtractor()
        symbols = extractor.extract(code, filter_names=["keep"])

        assert all(s.name == "keep" for s in symbols)

    def test_empty_content(self):
        extractor = RegexExtractor()
        symbols = extractor.extract("")

        assert symbols == []


class TestUnifiedInterface:
    """Tests for the unified extract_symbols entry point."""

    def test_python_file_uses_python_extractor(self):
        code = "def test(): pass"
        symbols, source = extract_symbols(code, "test.py")

        assert source == "py"
        assert len(symbols) == 1
        assert symbols[0].name == "test"

    def test_python_file_with_path(self):
        code = "def test(): pass"
        symbols, source = extract_symbols(code, "/path/to/module.py")

        assert source == "py"

    def test_markdown_uses_markdown_extractor(self):
        code = "# Header 1\n## Header 2"
        symbols, source = extract_symbols(code, "test.md")

        assert source == "md"
        assert len(symbols) == 2

    def test_unknown_extension_uses_regex(self):
        code = "def test(): pass"
        symbols, source = extract_symbols(code, "test.unknown")

        assert source == "regex"
        assert len(symbols) >= 1

    def test_no_extension_uses_regex(self):
        code = "def test(): pass"
        symbols, source = extract_symbols(code, "Makefile")

        assert source == "regex"

    def test_filter_names_passed_through(self):
        code = """
def keep():
    pass

def ignore():
    pass
"""
        symbols, _ = extract_symbols(code, "test.py", filter_names=["keep"])
        assert len(symbols) == 1
        assert symbols[0].name == "keep"


class TestMarkdownExtraction:
    """Tests for Markdown header extraction."""

    def test_extract_h1(self):
        code = "# Title"
        symbols, _ = extract_symbols(code, "test.md")

        assert any(s.signature == "# Title" for s in symbols)

    def test_extract_h2(self):
        code = "## Section"
        symbols, _ = extract_symbols(code, "test.md")

        assert any(s.signature == "## Section" for s in symbols)

    def test_extract_h3(self):
        code = "### Subsection"
        symbols, _ = extract_symbols(code, "test.md")

        assert any(s.signature == "### Subsection" for s in symbols)

    def test_extract_multiple_headers(self):
        code = """
# Title
## Section 1
### Subsection
## Section 2
"""
        symbols, _ = extract_symbols(code, "test.md")

        assert len(symbols) == 4


class TestMetadataHelpers:
    """Tests for plumbing_summary, first_export_preview, etc."""

    def test_build_plumbing_summary_with_exports(self):
        symbols = [
            ExtractedSymbol(
                name="func1", kind="function", line=1, signature="def func1()"
            ),
            ExtractedSymbol(
                name="func2", kind="function", line=2, signature="def func2()"
            ),
            ExtractedSymbol(
                name="MyClass", kind="class", line=3, signature="class MyClass:"
            ),
        ]
        summary = build_plumbing_summary(symbols)

        assert "Exports: func1, func2, MyClass" in summary

    def test_build_plumbing_summary_limits_exports(self):
        symbols = [
            ExtractedSymbol(
                name=f"func{i}", kind="function", line=i, signature=f"def func{i}()"
            )
            for i in range(5)
        ]
        summary = build_plumbing_summary(symbols)

        assert "..." in summary or len(summary.split(",")) <= 3

    def test_build_plumbing_summary_no_exports(self):
        symbols = []
        summary = build_plumbing_summary(symbols)

        assert summary == "No exports detected"

    def test_get_first_export_preview_function(self):
        symbols = [
            ExtractedSymbol(
                name="first", kind="function", line=1, signature="def first() -> int"
            ),
            ExtractedSymbol(
                name="second", kind="function", line=2, signature="def second()"
            ),
        ]
        preview = get_first_export_preview(symbols)

        assert preview == "def first() -> int"

    def test_get_first_export_preview_class(self):
        symbols = [
            ExtractedSymbol(
                name="MyClass", kind="class", line=1, signature="class MyClass:"
            ),
        ]
        preview = get_first_export_preview(symbols)

        assert preview == "class MyClass:"

    def test_get_first_export_preview_empty(self):
        symbols = []
        preview = get_first_export_preview(symbols)

        assert preview == ""

    def test_get_first_export_preview_capped_at_120_chars(self):
        long_sig = "def " + "x" * 200 + "():"
        symbols = [
            ExtractedSymbol(name="long", kind="function", line=1, signature=long_sig),
        ]
        preview = get_first_export_preview(symbols)

        assert len(preview) <= 120

    def test_get_export_signatures_all(self):
        symbols = [
            ExtractedSymbol(
                name="func1", kind="function", line=1, signature="def func1()"
            ),
            ExtractedSymbol(
                name="func2", kind="function", line=2, signature="def func2()"
            ),
        ]
        sigs = get_export_signatures(symbols)

        assert len(sigs) == 2
        assert "def func1()" in sigs
        assert "def func2()" in sigs

    def test_get_export_signatures_filtered(self):
        symbols = [
            ExtractedSymbol(
                name="keep", kind="function", line=1, signature="def keep()"
            ),
            ExtractedSymbol(
                name="ignore", kind="function", line=2, signature="def ignore()"
            ),
        ]
        sigs = get_export_signatures(symbols, filter_names=["keep"])

        assert len(sigs) == 1
        assert "def keep()" in sigs

    def test_get_export_signatures_skips_imports(self):
        symbols = [
            ExtractedSymbol(
                name="func", kind="function", line=1, signature="def func()"
            ),
            ExtractedSymbol(name="os", kind="import", line=2, signature="import os"),
        ]
        sigs = get_export_signatures(symbols)

        assert len(sigs) == 1
        assert "func" in sigs[0]


class TestExtractMetadataFields:
    """Tests for the high-level extract_metadata_fields function."""

    def test_python_metadata(self):
        code = """
def process(data: dict) -> list:
    return []

class Processor:
    pass
"""
        summary, preview, all_sigs = extract_metadata_fields(code, "test.py")

        assert "Exports: process, Processor" in summary
        assert "def process(data: dict) -> list" == preview
        assert len(all_sigs) == 2

    def test_empty_content(self):
        summary, preview, all_sigs = extract_metadata_fields("", "test.py")

        assert summary == "No exports detected"
        assert preview == ""
        assert all_sigs == []

    def test_syntax_error_falls_back_gracefully(self):
        code = "def broken(:"
        summary, preview, all_sigs = extract_metadata_fields(code, "test.py")

        # Should not raise, returns empty/defaults
        assert isinstance(summary, str)
        assert isinstance(preview, str)
        assert isinstance(all_sigs, list)


class TestExtractedSymbol:
    """Tests for the ExtractedSymbol dataclass."""

    def test_core_fields_required(self):
        symbol = ExtractedSymbol(
            name="test",
            kind="function",
            line=1,
            signature="def test():",
        )

        assert symbol.name == "test"
        assert symbol.kind == "function"
        assert symbol.line == 1
        assert symbol.signature == "def test():"

    def test_optional_fields_defaults(self):
        symbol = ExtractedSymbol(
            name="test",
            kind="function",
            line=1,
            signature="def test():",
        )

        assert symbol.type_annotation is None
        assert symbol.is_async is False
        assert symbol.is_exported is True
        assert symbol.visibility is None
        assert symbol.receiver is None
        assert symbol.decorators == []
        assert symbol.generics == []

    def test_optional_fields_set(self):
        symbol = ExtractedSymbol(
            name="test",
            kind="function",
            line=1,
            signature="async def test<T>() -> int",
            type_annotation="int",
            is_async=True,
            is_exported=True,
            visibility="public",
            receiver=None,
            decorators=["@decorator"],
            generics=["T"],
        )

        assert symbol.type_annotation == "int"
        assert symbol.is_async is True
        assert symbol.decorators == ["@decorator"]
        assert symbol.generics == ["T"]
