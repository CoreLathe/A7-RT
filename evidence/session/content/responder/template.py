"""Template string evaluation with variable substitution from request and session."""

import re
from typing import Any

TEMPLATE_PATTERN: str = r"\{\{\s*(\w+)\s*\}\}"


class TemplateSyntaxError(Exception):
    """Raised for malformed templates, such as unclosed delimiters."""

    pass


def evaluate_template(template: str, context: dict[str, Any]) -> str:
    """
    Evaluate a template string with variable substitution.

    Args:
        template: The template string containing {{variable}} placeholders
        context: A dictionary mapping variable names to values

    Returns:
        The template with variables substituted

    Raises:
        TemplateSyntaxError: If the template has unclosed {{ delimiters
    """
    if not template:
        return ""

    # Check for unclosed {{ (opening delimiter without closing }})
    # Count occurrences of {{ and }}
    open_count = template.count("{{")
    close_count = template.count("}}")

    # Find any {{ that doesn't have a matching }}
    pos = 0
    while True:
        open_pos = template.find("{{", pos)
        if open_pos == -1:
            break
        # Check if there's a closing }} after this {{
        close_pos = template.find("}}", open_pos)
        if close_pos == -1:
            raise TemplateSyntaxError(f"Unclosed template delimiter at position {open_pos}")
        pos = open_pos + 2

    # Perform variable substitution using regex
    def replace_var(match: re.Match) -> str:
        var_name = match.group(1)
        if var_name in context:
            value = context[var_name]
            return str(value)
        return ""  # Missing variable renders as empty string

    result = re.sub(TEMPLATE_PATTERN, replace_var, template)
    return result
