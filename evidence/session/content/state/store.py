"""In-memory session state store with TTL and namespace isolation."""

import importlib.util
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass

# Load Session from apitypes.core (guard against re-loading)
if "types_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("types_core", Path(__file__).parent.parent / "apitypes" / "core.py")
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
Session = sys.modules["types_core"].Session


@dataclass
class _SessionEntry:
    """Internal entry storing session with expiration metadata."""
    session: Session
    expires_at: float


class StateStore:
    """In-memory session store with TTL and namespace isolation.
    
    Thread-safe operations for concurrent create/get/update/cleanup.
    Namespace isolation ensures separate StateStore instances don't share data.
    """
    
    def __init__(self, namespace: str = "default"):
        """Initialize store with optional namespace for isolation.
        
        Args:
            namespace: Unique namespace to isolate this store's data
        """
        self._namespace = namespace
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = threading.Lock()
    
    def _is_expired(self, entry: _SessionEntry) -> bool:
        """Check if a session entry has expired."""
        import time
        return time.time() > entry.expires_at


def create_session(store: StateStore, ttl_seconds: int) -> Session:
    """Create a new session with unique ID and TTL.
    
    Args:
        store: The StateStore instance
        ttl_seconds: Time-to-live in seconds
        
    Returns:
        New Session instance with unique ID
    """
    import time
    
    session_id = str(uuid.uuid4())
    session = Session(id=session_id, state={})
    expires_at = time.time() + ttl_seconds
    
    with store._lock:
        store._sessions[session_id] = _SessionEntry(
            session=session,
            expires_at=expires_at
        )
    
    return session


def get_session(store: StateStore, session_id: str) -> Optional[Session]:
    """Get session by ID if it exists and hasn't expired.
    
    Args:
        store: The StateStore instance
        session_id: The session ID to look up
        
    Returns:
        Session if found and not expired, None otherwise
    """
    with store._lock:
        entry = store._sessions.get(session_id)
        if entry is None:
            return None
        
        if store._is_expired(entry):
            # Clean up expired session on access
            del store._sessions[session_id]
            return None
        
        return entry.session


def update_session(store: StateStore, session: Session) -> None:
    """Update session state in the store.
    
    Preserves the existing TTL. If session doesn't exist or has expired,
    this is a no-op (the session data is not persisted).
    
    Args:
        store: The StateStore instance
        session: Session with updated state
    """
    with store._lock:
        entry = store._sessions.get(session.id)
        if entry is None or store._is_expired(entry):
            return
        
        # Update the session while preserving expiration
        store._sessions[session.id] = _SessionEntry(
            session=session,
            expires_at=entry.expires_at
        )


def cleanup_expired(store: StateStore) -> int:
    """Remove all expired sessions and return count removed.
    
    Args:
        store: The StateStore instance
        
    Returns:
        Number of expired sessions removed
    """
    expired_ids = []
    
    with store._lock:
        for session_id, entry in list(store._sessions.items()):
            if store._is_expired(entry):
                expired_ids.append(session_id)
        
        for session_id in expired_ids:
            del store._sessions[session_id]
    
    return len(expired_ids)

def delete_session(store: StateStore, session_id: str) -> None:
    """Delete a session by ID from the store.
    
    Args:
        store: The StateStore instance
        session_id: The session ID to delete
    """
    with store._lock:
        if session_id in store._sessions:
            del store._sessions[session_id]
