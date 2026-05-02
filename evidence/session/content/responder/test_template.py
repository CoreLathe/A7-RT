"""Tests for responder.template - template string evaluation with variable substitution."""
import pytest
import re
import time

# Import the exports being tested (implementation doesn't exist yet)
from responder.template import (
    evaluate_template,
    TemplateSyntaxError,
    TEMPLATE_PATTERN,
)


class TestTemplatePattern:
    """Tests for TEMPLATE_PATTERN constant."""
    
    def test_pattern_matches_double_brace_syntax(self):
        """Pattern should match {{variable}} syntax."""
        matches = list(re.finditer(TEMPLATE_PATTERN, "Hello {{name}}!"))
        assert len(matches) == 1
        assert matches[0].group(1) == "name"
    
    def test_pattern_allows_whitespace_inside_braces(self):
        """Pattern should allow whitespace: {{ name }}."""
        matches = list(re.finditer(TEMPLATE_PATTERN, "Hello {{ name }}!"))
        assert len(matches) == 1
        assert matches[0].group(1) == "name"
    
    def test_pattern_allows_multiple_whitespace_variations(self):
        """Pattern should handle various whitespace amounts."""
        template = "{{a}} {{ b }} {{  c  }}"
        matches = list(re.finditer(TEMPLATE_PATTERN, template))
        assert len(matches) == 3
        assert matches[0].group(1) == "a"
        assert matches[1].group(1) == "b"
        assert matches[2].group(1) == "c"
    
    def test_pattern_ignores_single_braces(self):
        """Pattern should not match single braces {var}."""
        matches = list(re.finditer(TEMPLATE_PATTERN, "Hello {name}!"))
        assert len(matches) == 0
    
    def test_pattern_requires_word_characters(self):
        """Pattern should only match word characters (\\w+)."""
        # Should match simple names
        assert re.search(TEMPLATE_PATTERN, "{{valid_name}}")
        assert re.search(TEMPLATE_PATTERN, "{{name123}}")
        # Should not match non-word characters
        assert not re.search(TEMPLATE_PATTERN, "{{with-dash}}")
        assert not re.search(TEMPLATE_PATTERN, "{{with.space}}")
        assert not re.search(TEMPLATE_PATTERN, "{{with@symbol}}")


class TestEvaluateTemplate:
    """Tests for evaluate_template function."""
    
    def test_simple_variable_substitution(self):
        """Basic variable substitution works."""
        result = evaluate_template("Hello {{name}}!", {"name": "World"})
        assert result == "Hello World!"
    
    def test_multiple_variable_substitution(self):
        """Multiple variables in one template."""
        template = "{{greeting}} {{name}}, you are {{age}} years old."
        context = {"greeting": "Hello", "name": "Alice", "age": "30"}
        result = evaluate_template(template, context)
        assert result == "Hello Alice, you are 30 years old."
    
    def test_missing_variable_renders_empty_string(self):
        """Missing variables should render as empty string (guarantee)."""
        result = evaluate_template("Hello {{name}}!", {})
        assert result == "Hello !"
    
    def test_missing_variable_among_others(self):
        """Mixed present and missing variables."""
        result = evaluate_template("{{a}} {{b}} {{c}}", {"a": "A", "c": "C"})
        assert result == "A  C"
    
    def test_whitespace_inside_braces_ignored(self):
        """Whitespace inside {{ }} should be ignored."""
        result = evaluate_template("{{  name  }}", {"name": "test"})
        assert result == "test"
    
    def test_empty_template_returns_empty_string(self):
        """Empty template returns empty string."""
        result = evaluate_template("", {})
        assert result == ""
    
    def test_template_with_no_variables(self):
        """Template with no variables returns unchanged."""
        result = evaluate_template("Hello World!", {})
        assert result == "Hello World!"
    
    def test_variable_at_start(self):
        """Variable at start of template."""
        result = evaluate_template("{{greeting}} World!", {"greeting": "Hello"})
        assert result == "Hello World!"
    
    def test_variable_at_end(self):
        """Variable at end of template."""
        result = evaluate_template("Hello {{name}}", {"name": "World"})
        assert result == "Hello World"
    
    def test_adjacent_variables(self):
        """Variables next to each other."""
        result = evaluate_template("{{a}}{{b}}{{c}}", {"a": "X", "b": "Y", "c": "Z"})
        assert result == "XYZ"
    
    def test_empty_variable_name_not_matched(self):
        """Empty {{}} should not match pattern, left as-is."""
        result = evaluate_template("Hello {{}}!", {})
        assert result == "Hello {{}}!"
    
    def test_numeric_values_converted_to_string(self):
        """Numeric values in context should be stringified."""
        result = evaluate_template("Count: {{count}}", {"count": 42})
        assert result == "Count: 42"
    
    def test_none_value_renders_as_empty(self):
        """None value in context should render as empty string."""
        result = evaluate_template("Value: {{val}}", {"val": None})
        assert result == "Value: "


class TestTemplateSyntaxError:
    """Tests for TemplateSyntaxError exception."""
    
    def test_is_exception_subclass(self):
        """TemplateSyntaxError should be an Exception subclass."""
        assert issubclass(TemplateSyntaxError, Exception)
    
    def test_can_be_raised_and_caught(self):
        """TemplateSyntaxError can be raised and caught."""
        with pytest.raises(TemplateSyntaxError):
            raise TemplateSyntaxError("test error")
    
    def test_unclosed_delimiter_raises_error(self):
        """Unclosed {{ should raise TemplateSyntaxError."""
        with pytest.raises(TemplateSyntaxError):
            evaluate_template("Hello {{name", {})
    
    def test_unclosed_with_partial_content_raises_error(self):
        """Various unclosed delimiter patterns."""
        with pytest.raises(TemplateSyntaxError):
            evaluate_template("{{", {})
        with pytest.raises(TemplateSyntaxError):
            evaluate_template("{{ ", {})
        with pytest.raises(TemplateSyntaxError):
            evaluate_template("test {{var", {})
    
    def test_only_opening_brace_does_not_raise(self):
        """Single { should not trigger syntax error."""
        result = evaluate_template("Hello {name", {})
        assert result == "Hello {name"


class TestLinearTimeGuarantee:
    """Tests verifying linear time complexity guarantee."""
    
    def test_evaluation_time_scales_linearly(self):
        """Evaluation time should scale linearly with template size."""
        # Create templates of different sizes
        small_template = "x" * 100 + "{{var}}" + "x" * 100
        medium_template = "x" * 1000 + "{{var}}" + "x" * 1000
        large_template = "x" * 10000 + "{{var}}" + "x" * 10000
        
        context = {"var": "value"}
        
        # Warm up
        evaluate_template(small_template, context)
        
        # Time small template
        start = time.perf_counter()
        evaluate_template(small_template, context)
        small_time = time.perf_counter() - start
        
        # Time medium template (10x larger)
        start = time.perf_counter()
        evaluate_template(medium_template, context)
        medium_time = time.perf_counter() - start
        
        # Time large template (100x larger than small)
        start = time.perf_counter()
        evaluate_template(large_template, context)
        large_time = time.perf_counter() - start
        
        # Linear scaling: 10x size should take ~10x time
        # Allow generous margin (20x) for system variance
        assert medium_time < small_time * 20, "Medium template took too long relative to small"
        assert large_time < small_time * 200, "Large template took too long relative to small"


class TestEdgeCases:
    """Additional edge case tests."""
    
    def test_template_with_only_whitespace(self):
        """Template with only whitespace."""
        result = evaluate_template("   ", {})
        assert result == "   "
    
    def test_unicode_in_template_and_context(self):
        """Unicode characters should work."""
        result = evaluate_template("{{greeting}}", {"greeting": "こんにちは"})
        assert result == "こんにちは"
    
    def test_special_regex_characters_in_context(self):
        """Special regex chars in context values."""
        result = evaluate_template("{{pattern}}", {"pattern": "$test.*+?"})
        assert result == "$test.*+?"
    
    def test_nested_braces_in_context(self):
        """Braces in context values should not be interpreted."""
        result = evaluate_template("{{value}}", {"value": "{{nested}}"})
        assert result == "{{nested}}"
    
    def test_many_variables_performance(self):
        """Template with many variables should still be fast."""
        variables = {f"var_{i}": f"val_{i}" for i in range(100)}
        template = " ".join(f"{{{{{k}}}}}" for k in variables.keys())
        
        start = time.perf_counter()
        result = evaluate_template(template, variables)
        elapsed = time.perf_counter() - start
        
        assert elapsed < 1.0, "Many variables took too long"
        assert "val_0" in result
        assert "val_99" in result
