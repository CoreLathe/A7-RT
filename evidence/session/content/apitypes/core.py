"""Core domain types for the application."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Request:
    """HTTP Request representation."""
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "method": self.method,
            "path": self.path,
            "headers": dict(self.headers),
            "body": self.body.decode("utf-8", errors="replace") if self.body else ""
        }


@dataclass(frozen=True)
class Response:
    """HTTP Response representation."""
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status,
            "headers": dict(self.headers),
            "body": self.body.decode("utf-8", errors="replace") if self.body else ""
        }


@dataclass(frozen=True)
class MatchRule:
    """Rule for matching incoming requests."""
    path_pattern: str
    method: Optional[str] = None
    header_patterns: dict[str, str] = field(default_factory=dict)
    body_contains: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "path_pattern": self.path_pattern,
            "method": self.method,
            "header_patterns": dict(self.header_patterns),
            "body_contains": self.body_contains
        }


@dataclass(frozen=True)
class Template:
    """Template for response generation."""
    content: str
    content_type: str
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "content": self.content,
            "content_type": self.content_type
        }


@dataclass(frozen=True)
class Session:
    """User session with state storage."""
    id: str
    state: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "state": dict(self.state)
        }
