"""Optional HTTP health endpoint for Kubernetes probes."""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import structlog

logger = structlog.get_logger()


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health endpoints.

    Provides /healthz (liveness) and /readyz (readiness) endpoints
    for Kubernetes health probes.
    """

    # Class-level callable for readiness check
    ready_check: Callable[[], bool]

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default HTTP logging to avoid noise."""
        pass

    def do_GET(self) -> None:
        """Handle GET requests for health endpoints."""
        if self.path == "/healthz":
            # Liveness: process is running
            self._respond(200, b"ok")
        elif self.path == "/readyz":
            # Readiness: can reach APIs
            if self.ready_check():
                self._respond(200, b"ready")
            else:
                self._respond(503, b"not ready")
        else:
            self._respond(404, b"not found")

    def _respond(self, code: int, body: bytes) -> None:
        """Send HTTP response with status code and body."""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server(port: int, ready_check: Callable[[], bool]) -> ThreadingHTTPServer:
    """Start health server in background thread.

    Creates HTTP server listening on specified port with /healthz and /readyz
    endpoints. Server runs in a daemon thread so it doesn't block shutdown.

    Args:
        port: Port to listen on
        ready_check: Callable that returns True if service is ready

    Returns:
        HTTPServer instance (call shutdown() to stop)
    """
    # Create handler class with ready_check bound
    handler = type(
        "BoundHealthHandler",
        (HealthHandler,),
        {"ready_check": staticmethod(ready_check)},
    )

    server = ThreadingHTTPServer(("", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.info("Health server started", port=port)
    return server
