"""Responder package for template evaluation and response building."""

from .template import evaluate_template, TemplateSyntaxError, TEMPLATE_PATTERN
from .engine import ResponderEngine, register_template, build_response

__all__ = ["evaluate_template", "TemplateSyntaxError", "TEMPLATE_PATTERN", "ResponderEngine", "register_template", "build_response"]
