"""HTTP health endpoint for Kubernetes probes.

Both endpoints answer from state the update loop keeps; neither makes an
outbound call, so a probe cannot load the OPNsense or DNS APIs.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import structlog

logger = structlog.get_logger()


class HealthState:
    """What the update loop last did, readable from the HTTP thread."""

    def __init__(self, interval: int, verify_delay: float = 0) -> None:
        self._lock = threading.Lock()
        # A cycle can legitimately run long (provider retries, the wait before
        # verification), so the loop may go quiet for a while before liveness fails.
        self.stale_after = 3 * interval + 120 + verify_delay
        self._heartbeat = time.monotonic()
        self._last_cycle_ok: bool | None = None
        self._last_cycle_at: float | None = None

    def beat(self) -> None:
        """The loop is alive."""
        with self._lock:
            self._heartbeat = time.monotonic()

    def cycle_done(self, ok: bool) -> None:
        """A full update cycle finished."""
        with self._lock:
            now = time.monotonic()
            self._heartbeat = now
            self._last_cycle_ok = ok
            self._last_cycle_at = now

    def alive(self) -> bool:
        with self._lock:
            return time.monotonic() - self._heartbeat <= self.stale_after

    def ready(self) -> bool:
        with self._lock:
            if self._last_cycle_ok is None or self._last_cycle_at is None:
                return False
            return (
                self._last_cycle_ok and time.monotonic() - self._last_cycle_at <= self.stale_after
            )


class HealthHandler(BaseHTTPRequestHandler):
    """Serves /healthz (liveness) and /readyz (readiness)."""

    state: HealthState

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default HTTP logging to avoid noise."""

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(*((200, b"ok") if self.state.alive() else (503, b"stalled")))
        elif self.path == "/readyz":
            self._respond(*((200, b"ready") if self.state.ready() else (503, b"not ready")))
        else:
            self._respond(404, b"not found")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server(port: int, state: HealthState) -> ThreadingHTTPServer:
    """Start the health server in a daemon thread (call shutdown() to stop it)."""
    handler = type("BoundHealthHandler", (HealthHandler,), {"state": state})
    server = ThreadingHTTPServer(("", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server started", port=port)
    return server
