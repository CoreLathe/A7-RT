"""Pattern compilation and match rule parsing."""

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Callable

# Load MatchRule from apitypes.core (guard against re-loading)
if "types_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("types_core", Path(__file__).parent.parent / "apitypes" / "core.py")
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
MatchRule = sys.modules["types_core"].MatchRule

class PatternError(Exception):
    """Exception raised for invalid pattern syntax or malformed input."""
    pass


def compile_pattern(pattern: str) -> Callable[[str], bool]:
    """
    Compile a pattern string into a matcher function.
    
    Supports wildcards:
    - `*` matches any character sequence (including empty)
    - `?` matches exactly one character
    
    All other characters, including regex metacharacters like ., [, ], etc.
    are treated as literal characters.
    
    Args:
        pattern: The pattern string to compile
        
    Returns:
        A callable that takes a string input and returns bool match result
        
    Raises:
        PatternError: If the pattern syntax is invalid
        
    Guarantees:
        O(n) execution where n = input length
    """
    if not isinstance(pattern, str):
        raise PatternError(f"Pattern must be a string, got {type(pattern).__name__}")
    
    # Convert pattern to regex, escaping special regex chars except * and ?
    # Then convert * to .* and ? to .
    regex_parts = []
    for char in pattern:
        if char == '*':
            regex_parts.append('.*')
        elif char == '?':
            regex_parts.append('.')
        else:
            # Escape regex metacharacters, treat as literal
            regex_parts.append(re.escape(char))
    
    regex_pattern = '^' + ''.join(regex_parts) + '$'
    
    try:
        compiled_regex = re.compile(regex_pattern)
    except re.error as e:
        raise PatternError(f"Invalid pattern syntax: {e}")
    
    def matcher(input_str: str) -> bool:
        """Match input against compiled pattern. O(n) where n = len(input_str)."""
        if not isinstance(input_str, str):
            return False
        return bool(compiled_regex.match(input_str))
    
    return matcher


def parse_match_rule(raw: dict[str, Any]) -> MatchRule:
    """
    Parse a dictionary into a MatchRule dataclass.
    
    Args:
        raw: Dictionary containing match rule fields
            - path_pattern (str, required): Pattern for matching paths
            - method (str|None, optional): HTTP method to match
            - header_patterns (dict[str,str], optional): Headers to match
            - body_contains (str|None, optional): Body content to match
            
    Returns:
        MatchRule dataclass instance
        
    Raises:
        PatternError: If required fields are missing or invalid
        
    Example:
        >>> raw = {
        ...     "path_pattern": "/api/users/*",
        ...     "method": "GET",
        ...     "header_patterns": {"Content-Type": "application/json"},
        ...     "body_contains": "user_id"
        ... }
        >>> rule = parse_match_rule(raw)
    """
    if not isinstance(raw, dict):
        raise PatternError(f"Input must be a dictionary, got {type(raw).__name__}")
    
    # Required field: path_pattern
    if "path_pattern" not in raw:
        raise PatternError("Missing required field: 'path_pattern'")
    
    path_pattern = raw["path_pattern"]
    if not isinstance(path_pattern, str):
        raise PatternError(f"'path_pattern' must be a string, got {type(path_pattern).__name__}")
    
    # Optional field: method (str|None)
    method = raw.get("method")
    if method is not None and not isinstance(method, str):
        raise PatternError(f"'method' must be a string or None, got {type(method).__name__}")
    
    # Optional field: header_patterns (dict[str, str])
    header_patterns = raw.get("header_patterns", {})
    if header_patterns is None:
        header_patterns = {}
    if not isinstance(header_patterns, dict):
        raise PatternError(f"'header_patterns' must be a dict, got {type(header_patterns).__name__}")
    # Validate all values are strings
    for key, value in header_patterns.items():
        if not isinstance(value, str):
            raise PatternError(f"Header pattern value for '{key}' must be a string, got {type(value).__name__}")
    
    # Optional field: body_contains (str|None)
    body_contains = raw.get("body_contains")
    if body_contains is not None and not isinstance(body_contains, str):
        raise PatternError(f"'body_contains' must be a string or None, got {type(body_contains).__name__}")
    
    return MatchRule(
        path_pattern=path_pattern,
        method=method,
        header_patterns=dict(header_patterns),
        body_contains=body_contains
    )
