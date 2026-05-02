"""Matcher package for pattern matching and rule parsing."""

from .engine import CollisionError, MatcherEngine
from .parser import compile_pattern, parse_match_rule, PatternError

__all__ = ["CollisionError", "compile_pattern", "MatcherEngine", "parse_match_rule", "PatternError"]
