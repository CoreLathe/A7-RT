"""Request matching engine for evaluating requests against match rules."""

import importlib.util
import sys
from pathlib import Path
from typing import Optional

# Load types from apitypes.core (guard against re-loading)
if "types_core" not in sys.modules:
    _types_path = Path(__file__).parent.parent / "apitypes" / "core.py"
    _spec = importlib.util.spec_from_file_location("types_core", _types_path)
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
Request = sys.modules["types_core"].Request
MatchRule = sys.modules["types_core"].MatchRule

# Import pattern compilation from parser
from .parser import compile_pattern


class CollisionError(Exception):
    """Exception raised when overlapping patterns are registered."""

    pass


class MatcherEngine:
    """Engine for matching requests against registered rules.

    Rules are checked in priority order (higher number = higher priority).
    First matching rule wins.
    """

    def __init__(self):
        """Initialize the matcher engine with empty rule set."""
        # List of (priority, rule) tuples, kept sorted by priority descending
        self._rules: list[tuple[int, MatchRule]] = []

    def register_rule(self, rule: MatchRule, priority: int) -> None:
        """Register a rule with given priority.

        Args:
            rule: The MatchRule to register
            priority: Priority level (higher number = checked first)

        Raises:
            CollisionError: If the rule overlaps with an existing rule
        """
        # Check for collision with existing rules before registering
        for existing_priority, existing_rule in self._rules:
            if self._rules_overlap(existing_rule, rule):
                raise CollisionError(f"Rule {rule} overlaps with existing rule {existing_rule}")

        # Insert while maintaining sorted order (higher priority first)
        inserted = False
        for i, (p, _) in enumerate(self._rules):
            if priority > p:
                self._rules.insert(i, (priority, rule))
                inserted = True
                break

        if not inserted:
            self._rules.append((priority, rule))

    def match_request(self, request: Request) -> Optional[MatchRule]:
        """Match request against registered rules.

        Rules are checked in priority order. First matching rule wins.

        Args:
            request: The Request to match

        Returns:
            The first matching MatchRule, or None if no match
        """
        for priority, rule in self._rules:
            if self._rule_matches(rule, request):
                return rule
        return None

    def detect_collisions(self) -> list[tuple[MatchRule, MatchRule]]:
        """Detect all overlapping rule pairs.

        Returns:
            List of tuples containing overlapping rule pairs
        """
        collisions = []
        for i, (p1, rule1) in enumerate(self._rules):
            for p2, rule2 in self._rules[i + 1 :]:
                if self._rules_overlap(rule1, rule2):
                    collisions.append((rule1, rule2))
        return collisions

    def get_rules(self) -> list[MatchRule]:
        """Get all registered rules in priority order.

        Returns:
            List of MatchRule objects sorted by priority (highest first)
        """
        return [rule for priority, rule in self._rules]

    def _rule_matches(self, rule: MatchRule, request: Request) -> bool:
        """Check if a rule matches a request.

        Matching checks:
        - path_pattern: wildcard pattern against request.path
        - method: exact match, or rule.method is None (matches any)
        - header_patterns: all patterns must match corresponding headers (wildcards)
        - body_contains: substring must be in request.body (decoded as utf-8)

        Args:
            rule: The MatchRule to check
            request: The Request to match against

        Returns:
            True if rule matches request, False otherwise
        """
        # Check method match (None matches any method)
        if rule.method is not None and rule.method != request.method:
            return False

        # Check path pattern
        path_matcher = compile_pattern(rule.path_pattern)
        if not path_matcher(request.path):
            return False

        # Check header patterns (all must match)
        for header_name, header_pattern in rule.header_patterns.items():
            if header_name not in request.headers:
                return False
            header_matcher = compile_pattern(header_pattern)
            if not header_matcher(request.headers[header_name]):
                return False

        # Check body contains
        if rule.body_contains is not None:
            try:
                body_str = request.body.decode("utf-8", errors="replace")
            except (AttributeError, UnicodeDecodeError):
                body_str = str(request.body) if request.body else ""
            if rule.body_contains not in body_str:
                return False

        return True

    def _rules_overlap(self, rule1: MatchRule, rule2: MatchRule) -> bool:
        """Check if two rules could match the same request.

        Rules overlap if:
        - Methods overlap (same method or at least one is None)
        - Path patterns could match the same path
        - Header patterns are compatible (could match same headers)

        Args:
            rule1: First MatchRule
            rule2: Second MatchRule

        Returns:
            True if rules could match same request, False otherwise
        """
        # Check method overlap
        if rule1.method is not None and rule2.method is not None:
            if rule1.method != rule2.method:
                return False

        # Check path pattern overlap
        if not self._patterns_overlap(rule1.path_pattern, rule2.path_pattern):
            return False

        # Check header pattern overlap
        # Merge all header names from both rules
        all_header_names = set(rule1.header_patterns.keys()) | set(rule2.header_patterns.keys())
        for header_name in all_header_names:
            pattern1 = rule1.header_patterns.get(header_name)
            pattern2 = rule2.header_patterns.get(header_name)

            # If one rule doesn't care about this header, it could match anything
            if pattern1 is None or pattern2 is None:
                continue

            # Both have patterns for this header - check if they could match same value
            if not self._patterns_overlap(pattern1, pattern2):
                return False

        # Check body_contains overlap
        if rule1.body_contains is not None and rule2.body_contains is not None:
            # If one contains the other, they could match the same body
            # If neither contains the other, they might still overlap on some body
            # For collision detection, we consider them potentially overlapping
            # unless we can prove they never overlap (which is hard with substrings)
            pass  # Assume potential overlap for body_contains

        return True

    def _patterns_overlap(self, pattern1: str, pattern2: str) -> bool:
        """Check if two wildcard patterns could match the same string.

        This is a conservative check - returns True if patterns might overlap.

        Args:
            pattern1: First wildcard pattern
            pattern2: Second wildcard pattern

        Returns:
            True if patterns could match same string, False if definitely disjoint
        """
        # Simple heuristic: if patterns are identical, they overlap
        if pattern1 == pattern2:
            return True

        # Remove wildcards and check for common literal parts
        # This is conservative - returns True if there's any chance of overlap

        # Split patterns by wildcards
        parts1 = pattern1.split("*")
        parts2 = pattern2.split("*")

        # If both have no wildcards, they're either equal (already checked) or disjoint
        if len(parts1) == 1 and len(parts2) == 1:
            return False

        # Check if literal parts from one could appear in the other
        # This is a simplified check - patterns like /a/* and /*/b might both match /a/b

        # Get literal prefixes and suffixes
        prefix1 = parts1[0] if parts1 else ""
        prefix2 = parts2[0] if parts2 else ""

        # If prefixes are different and neither is empty, they might still overlap
        # via wildcards, so we need to be conservative

        # Check if one pattern's fixed parts could match the other's structure
        # For simplicity, we assume overlap unless we can prove disjointness

        # One case where they're definitely disjoint: different fixed prefixes
        # when the other doesn't start with *
        if prefix1 and prefix2 and not pattern1.startswith("*") and not pattern2.startswith("*"):
            # Find the shorter prefix
            min_len = min(len(prefix1), len(prefix2))
            if prefix1[:min_len] != prefix2[:min_len]:
                return False

        # Similarly for suffixes
        suffix1 = parts1[-1] if parts1 else ""
        suffix2 = parts2[-1] if parts2 else ""
        if suffix1 and suffix2 and not pattern1.endswith("*") and not pattern2.endswith("*"):
            min_len = min(len(suffix1), len(suffix2))
            if suffix1[-min_len:] != suffix2[-min_len:]:
                return False

        # Default to assuming overlap for complex cases
        return True
