"""Rate limiter for API requests."""

import time
from threading import Lock


class RateLimiter:
    """Simple token bucket rate limiter for API requests.

    Ensures minimum interval between requests to avoid hitting rate limits.
    Thread-safe implementation.
    """

    def __init__(self, requests_per_minute: int = 30) -> None:
        """Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests allowed per minute
        """
        self.min_interval = 60.0 / requests_per_minute
        # Ensure first request doesn't wait
        self.last_request = time.monotonic() - self.min_interval
        self._lock = Lock()

    def wait(self) -> None:
        """Wait if necessary to respect rate limit.

        Blocks until enough time has passed since the last request.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_request = time.monotonic()
