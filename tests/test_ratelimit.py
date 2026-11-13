"""Tests for rate limiter."""

from __future__ import annotations

import pytest

from wan_dns_sync.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patch RateLimiter's time module to a fake clock."""
    fake = FakeClock()
    monkeypatch.setattr("wan_dns_sync.ratelimit.time.monotonic", fake.monotonic)
    monkeypatch.setattr("wan_dns_sync.ratelimit.time.sleep", fake.sleep)
    return fake


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_first_request_no_wait(self, clock: FakeClock) -> None:
        limiter = RateLimiter(requests_per_minute=60)  # 1s interval
        limiter.wait()
        assert clock.slept == []

    def test_rate_limiting_waits(self, clock: FakeClock) -> None:
        limiter = RateLimiter(requests_per_minute=60)  # 1s interval
        limiter.wait()  # no wait
        limiter.wait()  # should sleep 1s
        assert clock.slept == [1.0]

    def test_rate_limiting_partial_wait(self, clock: FakeClock) -> None:
        limiter = RateLimiter(requests_per_minute=60)  # 1s interval
        limiter.wait()

        # Advance time by 0.4s, next call should sleep 0.6s
        clock.now += 0.4
        limiter.wait()

        assert clock.slept == [0.6]

    def test_no_wait_after_interval(self, clock: FakeClock) -> None:
        limiter = RateLimiter(requests_per_minute=60)  # 1s interval
        limiter.wait()

        # Advance by > interval
        clock.now += 1.5
        limiter.wait()

        assert clock.slept == []

    def test_default_rate(self, clock: FakeClock) -> None:
        limiter = RateLimiter()
        assert limiter.min_interval == 2.0  # 60/30
