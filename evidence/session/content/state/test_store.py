"""Tests for state.store - In-memory session state store with TTL and namespace isolation."""

import time
import threading
import importlib.util
import sys
from pathlib import Path

# Load types.core module (guard against re-loading)
if "types_core" not in sys.modules:
    _types_core_path = Path(__file__).parent.parent / "apitypes" / "core.py"
    spec = importlib.util.spec_from_file_location("types_core", _types_core_path)
    types_core = importlib.util.module_from_spec(spec)
    sys.modules["types_core"] = types_core
    spec.loader.exec_module(types_core)
Session = sys.modules["types_core"].Session

# Import the state store module (will fail until implemented)
from state.store import StateStore, create_session, get_session, update_session, cleanup_expired


class TestStateStoreCreation:
    """Test StateStore class initialization."""
    
    def test_create_empty_store(self):
        """StateStore can be created without arguments."""
        store = StateStore()
        assert store is not None
    
    def test_create_store_with_namespace(self):
        """StateStore can be created with a namespace."""
        store = StateStore(namespace="test_ns")
        assert store is not None


class TestCreateSession:
    """Test create_session function."""
    
    def test_create_session_returns_session(self):
        """create_session returns a Session with id and empty state."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        assert isinstance(session, Session)
        assert session.id
        assert isinstance(session.id, str)
        assert len(session.id) > 0
        assert session.state == {}
    
    def test_create_session_different_ids(self):
        """Each session gets a unique id."""
        store = StateStore()
        session1 = create_session(store, ttl_seconds=300)
        session2 = create_session(store, ttl_seconds=300)
        
        assert session1.id != session2.id
    
    def test_create_session_stores_in_store(self):
        """Created session can be retrieved."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        retrieved = get_session(store, session.id)
        assert retrieved is not None
        assert retrieved.id == session.id


class TestGetSession:
    """Test get_session function."""
    
    def test_get_existing_session(self):
        """get_session returns Session for valid session_id."""
        store = StateStore()
        created = create_session(store, ttl_seconds=300)
        
        retrieved = get_session(store, created.id)
        
        assert retrieved is not None
        assert retrieved.id == created.id
    
    def test_get_nonexistent_session_returns_none(self):
        """get_session returns None for unknown session_id."""
        store = StateStore()
        
        result = get_session(store, "nonexistent-id")
        
        assert result is None
    
    def test_get_expired_session_returns_none(self):
        """GUARANTEE: Expired sessions return None."""
        store = StateStore()
        session = create_session(store, ttl_seconds=1)  # 1 second TTL
        
        # Wait for expiration
        time.sleep(1.5)
        
        result = get_session(store, session.id)
        assert result is None
    
    def test_get_session_includes_state(self):
        """Retrieved session includes current state."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        # Update session with state
        updated = Session(id=session.id, state={"user": "alice", "count": 42})
        update_session(store, updated)
        
        # Retrieve and verify
        retrieved = get_session(store, session.id)
        assert retrieved.state == {"user": "alice", "count": 42}


class TestUpdateSession:
    """Test update_session function."""
    
    def test_update_session_stores_state(self):
        """update_session persists session state."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        updated = Session(id=session.id, state={"key": "value"})
        update_session(store, updated)
        
        retrieved = get_session(store, session.id)
        assert retrieved.state == {"key": "value"}
    
    def test_update_session_overwrites_state(self):
        """update_session replaces previous state."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        # First update
        update_session(store, Session(id=session.id, state={"a": 1}))
        # Second update
        update_session(store, Session(id=session.id, state={"b": 2}))
        
        retrieved = get_session(store, session.id)
        assert retrieved.state == {"b": 2}
    
    def test_update_session_returns_none(self):
        """update_session returns None."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        result = update_session(store, session)
        
        assert result is None


class TestNamespaceIsolation:
    """Test GUARANTEE: Namespace isolation between stores."""
    
    def test_session_isolated_by_namespace(self):
        """GUARANTEE: session_id A cannot access session_id B state."""
        store_a = StateStore(namespace="ns_a")
        store_b = StateStore(namespace="ns_b")
        
        # Create session in store A
        session_a = create_session(store_a, ttl_seconds=300)
        update_session(store_a, Session(id=session_a.id, state={"from": "a"}))
        
        # Same ID cannot be accessed from store B
        result = get_session(store_b, session_a.id)
        assert result is None
    
    def test_same_store_accessible(self):
        """Same namespace allows access."""
        store = StateStore(namespace="shared_ns")
        
        session = create_session(store, ttl_seconds=300)
        update_session(store, Session(id=session.id, state={"data": "value"}))
        
        # Same store can access
        retrieved = get_session(store, session.id)
        assert retrieved.state == {"data": "value"}


class TestCleanupExpired:
    """Test cleanup_expired function."""
    
    def test_cleanup_expired_removes_expired(self):
        """cleanup_expired removes expired sessions and returns count."""
        store = StateStore()
        
        # Create sessions
        session1 = create_session(store, ttl_seconds=1)
        session2 = create_session(store, ttl_seconds=300)
        
        # Wait for expiration
        time.sleep(1.5)
        
        # Create another session after expiration
        session3 = create_session(store, ttl_seconds=300)
        
        # Cleanup
        removed = cleanup_expired(store)
        
        assert removed == 1
        assert get_session(store, session1.id) is None
        assert get_session(store, session2.id) is not None
        assert get_session(store, session3.id) is not None
    
    def test_cleanup_expired_no_expired(self):
        """cleanup_expired returns 0 when no sessions expired."""
        store = StateStore()
        
        create_session(store, ttl_seconds=300)
        create_session(store, ttl_seconds=300)
        
        removed = cleanup_expired(store)
        
        assert removed == 0
    
    def test_cleanup_expired_empty_store(self):
        """cleanup_expired works on empty store."""
        store = StateStore()
        
        removed = cleanup_expired(store)
        
        assert removed == 0
    
    def test_cleanup_expired_multiple_expired(self):
        """cleanup_expired handles multiple expired sessions."""
        store = StateStore()
        
        create_session(store, ttl_seconds=1)
        create_session(store, ttl_seconds=1)
        create_session(store, ttl_seconds=300)
        
        time.sleep(1.5)
        
        removed = cleanup_expired(store)
        
        assert removed == 2


class TestThreadSafety:
    """Test GUARANTEE: Thread-safe operations."""
    
    def test_concurrent_create_session(self):
        """Multiple threads can create sessions safely."""
        store = StateStore()
        session_ids = []
        errors = []
        lock = threading.Lock()
        
        def create_sessions():
            try:
                for _ in range(10):
                    session = create_session(store, ttl_seconds=300)
                    with lock:
                        session_ids.append(session.id)
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=create_sessions) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(session_ids) == 50
        # All IDs unique
        assert len(set(session_ids)) == 50
    
    def test_concurrent_read_write(self):
        """Concurrent reads and writes are safe."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        errors = []
        
        def writer():
            try:
                for i in range(50):
                    update_session(store, Session(id=session.id, state={"count": i}))
            except Exception as e:
                errors.append(e)
        
        def reader():
            try:
                for _ in range(50):
                    get_session(store, session.id)
            except Exception as e:
                errors.append(e)
        
        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=writer))
            threads.append(threading.Thread(target=reader))
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        # Final state should be valid
        final = get_session(store, session.id)
        assert final is not None
    
    def test_concurrent_cleanup_access(self):
        """Cleanup during concurrent access is safe."""
        store = StateStore()
        errors = []
        
        def creator():
            try:
                for _ in range(20):
                    create_session(store, ttl_seconds=1)
            except Exception as e:
                errors.append(e)
        
        def cleaner():
            try:
                for _ in range(10):
                    cleanup_expired(store)
                    time.sleep(0.1)
            except Exception as e:
                errors.append(e)
        
        def accessor():
            try:
                for _ in range(20):
                    cleanup_expired(store)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=creator),
            threading.Thread(target=cleaner),
            threading.Thread(target=accessor),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0


class TestSessionStateTypes:
    """Test session state supports various data types."""
    
    def test_state_string_values(self):
        """State supports string values."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        update_session(store, Session(id=session.id, state={"name": "test"}))
        retrieved = get_session(store, session.id)
        
        assert retrieved.state["name"] == "test"
    
    def test_state_numeric_values(self):
        """State supports numeric values."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        update_session(store, Session(id=session.id, state={
            "int": 42,
            "float": 3.14
        }))
        retrieved = get_session(store, session.id)
        
        assert retrieved.state["int"] == 42
        assert retrieved.state["float"] == 3.14
    
    def test_state_nested_dict(self):
        """State supports nested dictionaries."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        update_session(store, Session(id=session.id, state={
            "nested": {"a": 1, "b": 2}
        }))
        retrieved = get_session(store, session.id)
        
        assert retrieved.state["nested"] == {"a": 1, "b": 2}
    
    def test_state_list_values(self):
        """State supports list values."""
        store = StateStore()
        session = create_session(store, ttl_seconds=300)
        
        update_session(store, Session(id=session.id, state={
            "items": [1, 2, 3]
        }))
        retrieved = get_session(store, session.id)
        
        assert retrieved.state["items"] == [1, 2, 3]
