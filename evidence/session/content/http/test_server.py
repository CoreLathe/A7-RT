"""Tests for http.server module - HTTP server with routing, middleware, and graceful shutdown."""

import importlib.util
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from apitypes.core import Request, Response
from config.schema import DEFAULT_CONFIG, Config
from matcher.engine import MatcherEngine
from responder.engine import ResponderEngine
from state.store import StateStore

_server_path = Path(__file__).parent / "server.py"
spec = importlib.util.spec_from_file_location("http_server", _server_path)
http_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(http_server)


class TestCreateServer:
    def test_create_server_returns_httpserver_instance(self):
        config = Config(
            port=8080, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock()
        server = http_server.create_server(config, handler)
        assert isinstance(server, http_server.HTTPServer)

    def test_create_server_stores_config(self):
        config = Config(
            port=9090, host="0.0.0.0", log_level="DEBUG", ttl_default=1800, max_sessions=500
        )
        handler = Mock()
        server = http_server.create_server(config, handler)
        assert server.config == config

    def test_create_server_stores_handler(self):
        config = DEFAULT_CONFIG

        def my_handler(request):
            return Response(status=200, headers={}, body=b"OK")

        server = http_server.create_server(config, my_handler)
        assert server.handler == my_handler

    def test_create_server_with_optional_engines(self):
        config = DEFAULT_CONFIG
        handler = Mock()
        matcher = MatcherEngine()
        responder = ResponderEngine()
        store = StateStore()
        server = http_server.create_server(
            config, handler, matcher_engine=matcher, responder_engine=responder, state_store=store
        )
        assert server.matcher_engine == matcher
        assert server.responder_engine == responder
        assert server.state_store == store


class TestRunServer:
    def test_run_server_starts_listening(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock(return_value=Response(status=200, headers={}, body=b"OK"))
        server = http_server.create_server(config, handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        assert server.is_running
        assert server.port > 0
        http_server.shutdown_server(server)

    def test_run_server_port_binding_failure_raises_ioerror(self):
        config = Config(
            port=80, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock()
        server = http_server.create_server(config, handler)
        with pytest.raises(IOError):
            http_server.run_server(server)

    def test_run_server_already_running_raises_error(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock(return_value=Response(status=200, headers={}, body=b"OK"))
        server = http_server.create_server(config, handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        with pytest.raises(RuntimeError):
            http_server.run_server(server)
        http_server.shutdown_server(server)


class TestShutdownServer:
    def test_shutdown_server_stops_listening(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock(return_value=Response(status=200, headers={}, body=b"OK"))
        server = http_server.create_server(config, handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        assert server.is_running
        http_server.shutdown_server(server)
        time.sleep(0.1)
        assert not server.is_running

    def test_shutdown_server_graceful_completion(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        completion_event = threading.Event()

        def slow_handler(request):
            time.sleep(0.2)
            completion_event.set()
            return Response(status=200, headers={}, body=b"Completed")

        server = http_server.create_server(config, slow_handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)

        def send_request():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", server.port))
            sock.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            sock.recv(4096)
            sock.close()

        request_thread = threading.Thread(target=send_request)
        request_thread.start()
        time.sleep(0.05)
        http_server.shutdown_server(server)
        assert completion_event.wait(timeout=1.0)

    def test_shutdown_server_not_running_raises_error(self):
        config = DEFAULT_CONFIG
        handler = Mock()
        server = http_server.create_server(config, handler)
        with pytest.raises(RuntimeError):
            http_server.shutdown_server(server)


class TestHTTPServerClass:
    def test_server_has_required_attributes(self):
        config = DEFAULT_CONFIG

        def my_handler(request):
            return Response(status=200, headers={}, body=b"OK")

        server = http_server.create_server(config, my_handler)
        assert hasattr(server, "config")
        assert hasattr(server, "handler")
        assert hasattr(server, "is_running")
        assert server.is_running is False

    def test_server_port_property(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock(return_value=Response(status=200, headers={}, body=b"OK"))
        server = http_server.create_server(config, handler)
        with pytest.raises(RuntimeError):
            _ = server.port
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        assert server.port > 0
        http_server.shutdown_server(server)


class TestRequestHandling:
    def test_server_calls_handler_with_request(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        received_request = None

        def capturing_handler(request):
            nonlocal received_request
            received_request = request
            return Response(status=200, headers={"Content-Type": "text/plain"}, body=b"OK")

        server = http_server.create_server(config, capturing_handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", server.port))
        sock.send(
            b'POST /api/test HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{"key":"val"}'
        )
        response = sock.recv(4096)
        sock.close()
        time.sleep(0.1)
        assert received_request is not None
        assert received_request.method == "POST"
        assert received_request.path == "/api/test"
        http_server.shutdown_server(server)

    def test_server_returns_handler_response(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )

        def handler(request):
            return Response(
                status=201,
                headers={"Content-Type": "application/json", "X-Custom": "value"},
                body=b'{"created": true}',
            )

        server = http_server.create_server(config, handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", server.port))
        sock.send(b"GET /test HTTP/1.1\r\nHost: localhost\r\n\r\n")
        response = sock.recv(4096)
        sock.close()
        response_str = response.decode("utf-8", errors="replace")
        assert "201" in response_str
        assert "application/json" in response_str
        assert "X-Custom" in response_str
        http_server.shutdown_server(server)

    def test_request_timeout_after_30_seconds(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )

        def slow_handler(request):
            time.sleep(35)
            return Response(status=200, headers={}, body=b"Too late")

        server = http_server.create_server(config, slow_handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(32)
        sock.connect(("127.0.0.1", server.port))
        sock.send(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
        response = sock.recv(4096)
        sock.close()
        response_str = response.decode("utf-8", errors="replace")
        assert "408" in response_str or "504" in response_str or "timeout" in response_str.lower()
        http_server.shutdown_server(server)

    def test_concurrent_request_handling(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        active_requests = threading.Lock()
        concurrent_count = 0
        max_concurrent = 0

        def handler(request):
            nonlocal concurrent_count, max_concurrent
            with active_requests:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            time.sleep(0.2)
            with active_requests:
                concurrent_count -= 1
            return Response(status=200, headers={}, body=b"OK")

        server = http_server.create_server(config, handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)

        def send_request():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", server.port))
            sock.send(b"GET /test HTTP/1.1\r\nHost: localhost\r\n\r\n")
            sock.recv(4096)
            sock.close()

        threads = [threading.Thread(target=send_request) for _ in range(5)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        assert max_concurrent >= 2
        for t in threads:
            t.join(timeout=2)
        http_server.shutdown_server(server)


class TestMiddlewareChain:
    def test_server_supports_middleware_chain(self):
        config = DEFAULT_CONFIG
        middleware_order = []

        def middleware1(handler):
            def wrapper(request):
                middleware_order.append("before1")
                response = handler(request)
                middleware_order.append("after1")
                return response

            return wrapper

        def middleware2(handler):
            def wrapper(request):
                middleware_order.append("before2")
                response = handler(request)
                middleware_order.append("after2")
                return response

            return wrapper

        def base_handler(request):
            middleware_order.append("handler")
            return Response(status=200, headers={}, body=b"OK")

        server = http_server.create_server(config, base_handler)
        server.add_middleware(middleware1)
        server.add_middleware(middleware2)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", server.port))
        sock.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        sock.recv(4096)
        sock.close()
        assert "before2" in middleware_order
        assert "before1" in middleware_order
        assert "handler" in middleware_order
        assert middleware_order.index("before2") < middleware_order.index("before1")
        assert middleware_order.index("before1") < middleware_order.index("handler")
        http_server.shutdown_server(server)


class TestSignalHandling:
    def test_graceful_shutdown_on_sigterm(self):
        config = Config(
            port=0, host="127.0.0.1", log_level="INFO", ttl_default=3600, max_sessions=1000
        )
        handler = Mock(return_value=Response(status=200, headers={}, body=b"OK"))
        server = http_server.create_server(config, handler)
        server_thread = threading.Thread(target=http_server.run_server, args=(server,), daemon=True)
        server_thread.start()
        time.sleep(0.1)
        assert server.is_running
        signal.raise_signal(signal.SIGTERM)
        time.sleep(0.2)
        assert not server.is_running
