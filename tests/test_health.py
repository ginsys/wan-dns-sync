"""Tests for the health endpoints."""

from __future__ import annotations

import httpx
import pytest

from wan_dns_sync.health import HealthState, start_health_server


class TestHealthState:
    def test_liveness_allows_for_the_wait_before_verification(self) -> None:
        """A cycle that sleeps verify_delay must not look hung."""
        assert (
            HealthState(interval=60, verify_delay=120).stale_after
            == HealthState(interval=60).stale_after + 120
        )

    def test_not_ready_before_the_first_cycle(self) -> None:
        state = HealthState(interval=60)
        assert state.alive() is True
        assert state.ready() is False

    def test_ready_after_a_good_cycle(self) -> None:
        state = HealthState(interval=60)
        state.cycle_done(True)
        assert state.ready() is True

    def test_not_ready_after_a_failed_cycle(self) -> None:
        state = HealthState(interval=60)
        state.cycle_done(False)
        assert state.ready() is False
        assert state.alive() is True

    def test_stalled_loop_is_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr("wan_dns_sync.health.time.monotonic", lambda: now[0])
        state = HealthState(interval=60)
        now[0] += state.stale_after + 1
        assert state.alive() is False
        state.beat()
        assert state.alive() is True

    def test_old_cycle_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr("wan_dns_sync.health.time.monotonic", lambda: now[0])
        state = HealthState(interval=60)
        state.cycle_done(True)
        now[0] += state.stale_after + 1
        assert state.ready() is False


class TestEndpoints:
    def test_endpoints_answer_from_state(self) -> None:
        state = HealthState(interval=60)
        server = start_health_server(0, state)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            assert httpx.get(f"{base}/healthz").status_code == 200
            assert httpx.get(f"{base}/readyz").status_code == 503
            state.cycle_done(True)
            assert httpx.get(f"{base}/readyz").status_code == 200
            assert httpx.get(f"{base}/other").status_code == 404
        finally:
            server.shutdown()
            server.server_close()
