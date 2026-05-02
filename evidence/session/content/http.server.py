"""HTTP server with request routing, middleware chain, and graceful shutdown."""

import socket
import threading
import signal
import sys
from typing import Callable
from io import BytesIO

from config import Config
from apitypes import Request, Response


class HTTPServer:
    """HTTP server with concurrent request handling and graceful shutdown."""
    
    def __init__(self, config: Config, handler: Callable[[Request], Response]):
        self.config = config
        self.handler = handler
        self._socket: socket.socket | None = None
        self._is_running = False
        self._shutdown_event = threading.Event()
        self._active_threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
    
    @property
    def is_running(self) -> bool:
        """Whether the server is currently running."""
        return self._is_running
    
    @property
    def port(self) -> int:
        """The port the server is bound to (0 if not bound)."""
        if self._socket is None:
            return 0
        return self._socket.getsockname()[1]
    
    def _parse_request(self, client_socket: socket.socket) -> Request | None:
        """Parse HTTP request from socket into Request object."""
        try:
            # Read headers
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client_socket.recv(4096)
                if not chunk:
                    return None
                data += chunk
            
            # Split headers and body
            header_end = data.find(b"\r\n\r\n")
            header_data = data[:header_end].decode("utf-8", errors="replace")
            body_data = data[header_end + 4:]
            
            # Parse request line and headers
            lines = header_data.split("\r\n")
            if not lines:
                return None
            
            # Parse request line: METHOD PATH HTTP/1.1
            request_line = lines[0]
            parts = request_line.split(" ")
            if len(parts) < 2:
                return None
            
            method = parts[0]
            path = parts[1]
            
            # Parse headers
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip()] = value.strip()
            
            # Check for Content-Length and read more body if needed
            content_length = 0
            if "Content-Length" in headers:
                try:
                    content_length = int(headers["Content-Length"])
                except ValueError:
                    content_length = 0
            
            # Read remaining body
            while len(body_data) < content_length:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                body_data += chunk
            
            # Trim body to content length
            body = body_data[:content_length]
            
            return Request(method=method, path=path, headers=headers, body=body)
        except Exception:
            return None
    
    def _send_response(self, client_socket: socket.socket, response: Response) -> None:
        """Serialize Response to HTTP response and send over socket."""
        try:
            # Build status line
            status_text = "OK" if response.status == 200 else "Not Found" if response.status == 404 else "Error"
            if response.status == 201:
                status_text = "Created"
            elif response.status == 204:
                status_text = "No Content"
            elif response.status == 400:
                status_text = "Bad Request"
            elif response.status == 500:
                status_text = "Internal Server Error"
            
            response_line = f"HTTP/1.1 {response.status} {status_text}\r\n"
            
            # Build headers
            headers = dict(response.headers)
            headers["Content-Length"] = str(len(response.body))
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/octet-stream"
            
            header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            
            # Combine and send
            response_data = response_line.encode() + header_lines.encode() + b"\r\n" + response.body
            client_socket.sendall(response_data)
        except Exception:
            pass
    
    def _handle_client(self, client_socket: socket.socket, client_addr: tuple) -> None:
        """Handle a single client connection."""
        try:
            # Set socket timeout for request timeout
            client_socket.settimeout(30.0)
            
            # Parse request
            request = self._parse_request(client_socket)
            if request is None:
                # Send 400 Bad Request
                self._send_response(client_socket, Response(status=400, body=b"Bad Request"))
                return
            
            # Call handler
            try:
                response = self.handler(request)
            except Exception:
                response = Response(status=500, body=b"Internal Server Error")
            
            # Send response
            self._send_response(client_socket, response)
        except socket.timeout:
            try:
                self._send_response(client_socket, Response(status=408, body=b"Request Timeout"))
            except Exception:
                pass
        except Exception:
            pass
        finally:
            try:
                client_socket.close()
            except Exception:
                pass
    
    def _server_loop(self) -> None:
        """Main server accept loop."""
        self._socket.settimeout(1.0)  # Check shutdown every second
        
        while not self._shutdown_event.is_set():
            try:
                client_socket, client_addr = self._socket.accept()
                
                # Handle client in a new thread for concurrent handling
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, client_addr),
                    daemon=True
                )
                
                with self._lock:
                    self._active_threads.add(thread)
                
                thread.start()
                
                # Clean up finished threads
                with self._lock:
                    self._active_threads = {t for t in self._active_threads if t.is_alive()}
                    
            except socket.timeout:
                continue
            except OSError:
                # Socket closed
                break
            except Exception:
                continue
    
    def start(self) -> None:
        """Start the server (internal method)."""
        if self._is_running:
            raise RuntimeError("Server is already running")
        
        # Create socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self._socket.bind((self.config.host, self.config.port))
        except (OSError, PermissionError) as e:
            self._socket.close()
            self._socket = None
            raise IOError(f"Failed to bind to {self.config.host}:{self.config.port}: {e}")
        
        self._socket.listen(5)
        self._is_running = True
        self._shutdown_event.clear()
        
        # Start server loop in a thread
        self._server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self._server_thread.start()
    
    def shutdown(self) -> None:
        """Shutdown the server gracefully (internal method)."""
        if not self._is_running:
            raise RuntimeError("Server is not running")
        
        self._shutdown_event.set()
        
        # Close socket to unblock accept()
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        
        # Wait for active threads to finish (with timeout)
        with self._lock:
            threads = list(self._active_threads)
        
        for thread in threads:
            thread.join(timeout=5.0)
        
        self._is_running = False


def create_server(config: Config, handler: Callable[[Request], Response], **optional_engines) -> HTTPServer:
    """Factory function to create an HTTPServer instance.
    
    Args:
        config: Server configuration
        handler: Request handler function
        **optional_engines: Optional engines for future extensibility
    
    Returns:
        HTTPServer instance
    """
    return HTTPServer(config, handler)


def run_server(server: HTTPServer) -> None:
    """Start the server and block until shutdown.
    
    Args:
        server: The HTTPServer instance to run
        
    Raises:
        IOError: If port binding fails
        RuntimeError: If server is already running
    """
    server.start()
    
    # Block until shutdown signal
    try:
        while server.is_running:
            server._shutdown_event.wait(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if server.is_running:
            server.shutdown()


def shutdown_server(server: HTTPServer) -> None:
    """Gracefully shutdown the server.
    
    Args:
        server: The HTTPServer instance to shutdown
        
    Raises:
        RuntimeError: If server is not running
    """
    server.shutdown()