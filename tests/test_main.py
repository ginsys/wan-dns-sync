"""Tests for the update cycle and the process exit code."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.conftest import make_config
from tests.fakes import FakeProvider
from wan_dns_sync import main as main_module
from wan_dns_sync.config import Config
from wan_dns_sync.main import Updater

ZONE = "example.com"
IPS = {"wan1": "1.2.3.4", "wan2": "5.6.7.8"}


class Harness:
    """An Updater wired to one fake provider and scripted OPNsense answers."""

    def __init__(self, config: Config | None = None, ips: dict[str, str] | None = None) -> None:
        self.config = config or make_config()
        self.provider = FakeProvider("fake", {ZONE: {}})
        self.loads = 0
        self.ips: dict[str, str] | Exception = IPS if ips is None else ips
        self.verified: list[tuple[str, str, set[str]]] = []
        self.verify_result = True
        self.annotations: list[dict[str, Any]] = []
        self.annotate_error: Exception | None = None
        self.now = 0.0
        opnsense = MagicMock()
        opnsense.get_interface_ips.side_effect = self._ips
        self.updater = Updater(
            self.config,
            opnsense,
            dry_run=False,
            provider_loader=self._load,
            verify=self._verify,
            annotate=self._annotate,
            sleep=lambda _: None,
            clock=lambda: self.now,
        )

    def _ips(self) -> dict[str, str]:
        if isinstance(self.ips, Exception):
            raise self.ips
        return self.ips

    def _load(self, name: str, args: dict[str, Any]) -> FakeProvider:
        self.loads += 1
        return self.provider

    def _verify(self, name: str, zone: str, ips: set[str]) -> bool:
        self.verified.append((name, zone, ips))
        return self.verify_result

    def _annotate(self, **kwargs: Any) -> bool:
        if self.annotate_error:
            raise self.annotate_error
        self.annotations.append(kwargs)
        return True

    @property
    def stored(self) -> dict[tuple[str, str], dict[str, Any]]:
        return self.provider.zones[ZONE]


def test_first_cycle_writes_every_record() -> None:
    h = Harness()

    assert h.updater.run_cycle() is True

    assert h.stored[("test", "A")]["values"] == ["1.2.3.4"]
    assert h.stored[("multi", "A")]["values"] == ["1.2.3.4", "5.6.7.8"]
    assert {v[0] for v in h.verified} == {"test", "multi"}


def test_unchanged_addresses_skip_the_provider() -> None:
    h = Harness()
    h.updater.run_cycle()
    loads = h.loads

    h.now = 60
    assert h.updater.run_cycle() is True

    assert h.loads == loads


def test_reconcile_after_the_interval_corrects_drift() -> None:
    h = Harness()
    h.updater.run_cycle()
    h.stored[("test", "A")]["values"] = ["9.9.9.9"]  # someone edited it by hand

    h.now = 3601
    assert h.updater.run_cycle() is True

    assert h.stored[("test", "A")]["values"] == ["1.2.3.4"]


def test_changed_address_is_written_before_the_reconcile_interval() -> None:
    h = Harness()
    h.updater.run_cycle()

    h.ips = {"wan1": "1.1.1.1", "wan2": "5.6.7.8"}
    h.now = 60
    assert h.updater.run_cycle() is True

    assert h.stored[("test", "A")]["values"] == ["1.1.1.1"]


def test_record_without_an_address_is_skipped_not_deleted() -> None:
    h = Harness()
    h.updater.run_cycle()

    h.ips = {"wan2": "5.6.7.8"}  # wan1 is down: "test" has no address, "multi" has one
    h.now = 60
    assert h.updater.run_cycle() is True

    assert h.stored[("test", "A")]["values"] == ["1.2.3.4"]
    assert h.stored[("multi", "A")]["values"] == ["5.6.7.8"]


def test_opnsense_failure_fails_the_cycle_and_writes_nothing() -> None:
    h = Harness()
    h.ips = RuntimeError("router unreachable")

    assert h.updater.run_cycle() is False

    assert h.stored == {}


def test_no_interface_ips_fails_the_cycle() -> None:
    h = Harness(ips={})

    assert h.updater.run_cycle() is False


def test_provider_failure_fails_the_cycle_and_retries_in_full() -> None:
    h = Harness()
    h.provider.fail_apply = RuntimeError("API down")
    assert h.updater.run_cycle() is False

    h.provider.fail_apply = None
    h.now = 60  # well before the reconcile interval
    assert h.updater.run_cycle() is True

    assert h.stored[("test", "A")]["values"] == ["1.2.3.4"]


def test_failure_after_an_earlier_success_is_retried_when_the_addresses_flap_back() -> None:
    """A failed write may leave the zone half changed, even when the next cycle wants the
    addresses that were last applied; skipping that cycle would leave it that way."""
    h = Harness()
    assert h.updater.run_cycle() is True  # applied: wan1 = 1.2.3.4

    h.ips = {"wan1": "1.1.1.1", "wan2": "5.6.7.8"}
    h.provider.fail_apply = RuntimeError("API down")
    h.now = 60
    assert h.updater.run_cycle() is False

    h.provider.fail_apply = None
    h.ips = IPS  # back to the last applied addresses, well before the reconcile interval
    h.provider.zones[ZONE][("test", "A")]["values"] = ["1.1.1.1"]  # what the failed write left
    h.now = 120
    assert h.updater.run_cycle() is True

    assert h.stored[("test", "A")]["values"] == ["1.2.3.4"]


def test_failed_verification_is_retried_until_it_passes() -> None:
    h = Harness()
    h.verify_result = False
    assert h.updater.run_cycle() is False
    assert h.updater.run_cycle() is False  # the zone now matches, but is still unverified
    verified_before = len(h.verified)

    h.verify_result = True
    assert h.updater.run_cycle() is True

    assert len(h.verified) > verified_before
    h.verified.clear()
    assert h.updater.run_cycle() is True
    assert h.verified == []  # verified once, not again


def test_pending_verification_survives_a_record_losing_its_interface() -> None:
    h = Harness()
    h.verify_result = False
    h.updater.run_cycle()  # "test" and "multi" written, not verified

    h.verify_result = True
    h.ips = {"wan2": "5.6.7.8"}  # wan1 is down: "test" has no address this cycle
    h.now = 60
    h.verified.clear()
    assert h.updater.run_cycle() is True

    assert ("test", ZONE, {"1.2.3.4"}) in h.verified  # still checked, against what was written
    assert h.updater._unverified == {}  # checked only until it passed


def test_a_write_whose_response_was_lost_is_verified_on_the_retry() -> None:
    h = Harness()
    h.verify_result = False
    h.provider.fail_after_apply = RuntimeError("timeout after the write")
    assert h.updater.run_cycle() is False

    h.provider.fail_after_apply = None
    h.verify_result = True
    h.verified.clear()
    h.now = 60
    assert h.updater.run_cycle() is True  # the provider already matches: nothing to apply

    assert {v[0] for v in h.verified} == {"test", "multi"}


def test_a_failure_before_any_write_leaves_nothing_pending() -> None:
    h = Harness()
    assert h.updater.run_cycle() is True  # 1.2.3.4 published and verified

    h.ips = {"wan1": "9.9.9.9", "wan2": "5.6.7.8"}
    healthy = h.updater._load_provider
    h.updater._load_provider = lambda name, args: (_ for _ in ()).throw(RuntimeError("no token"))
    h.now = 60
    assert h.updater.run_cycle() is False  # the provider could not even be loaded
    assert h.updater._unverified == {}

    h.updater._load_provider = healthy
    h.ips = IPS  # back to what is published: nothing to write, nothing to wait for
    h.now = 120
    assert h.updater.run_cycle() is True


def test_an_uncertain_write_that_never_landed_is_not_waited_for() -> None:
    h = Harness()
    # the nameservers serve exactly what the provider holds
    h.updater._verify = lambda name, zone, ips: ips == set(h.stored[(name, "A")]["values"])
    assert h.updater.run_cycle() is True  # 1.2.3.4 published and verified

    h.ips = {"wan1": "9.9.9.9", "wan2": "5.6.7.8"}
    h.provider.fail_apply = RuntimeError("503 before the write")
    h.now = 60
    assert h.updater.run_cycle() is False
    assert h.updater._unverified  # it may have landed, so it is checked

    h.provider.fail_apply = None
    h.ips = IPS  # the provider still holds 1.2.3.4, which is what is wanted again
    h.now = 120
    assert h.updater.run_cycle() is True  # the stale 9.9.9.9 expectation was dropped
    assert h.updater._unverified == {}


def test_each_syncing_cycle_builds_a_fresh_provider() -> None:
    """A provider instance caches a zone's records, so none may live across cycles."""
    h = Harness()
    h.updater.run_cycle()
    first = h.loads

    h.now = 3601  # reconcile due
    h.updater.run_cycle()

    assert h.loads == 2 * first


def test_annotations_are_reconciled_when_dns_is_skipped() -> None:
    config = make_config(
        kubernetes={"enabled": True, "label_selector": "a=b", "interfaces": ["wan1"]}
    )
    h = Harness(config)
    h.updater.run_cycle()
    loads = h.loads

    h.now = 60  # unchanged addresses: DNS is skipped
    h.updater.run_cycle()

    assert h.loads == loads
    assert len(h.annotations) == 2


def test_one_failing_provider_does_not_stop_the_other() -> None:
    config = make_config(
        providers={
            "bad": {"class": "tests.fakes.FakeProvider"},
            "good": {"class": "tests.fakes.FakeProvider"},
        },
        zones={
            ZONE: {
                "providers": ["bad", "good"],
                "records": [{"name": "test", "interfaces": ["wan1"]}],
            }
        },
    )
    h = Harness(config)
    good = FakeProvider("good", {ZONE: {}})
    bad = FakeProvider("bad", {ZONE: {}})
    bad.fail_apply = RuntimeError("boom")
    h.updater._load_provider = lambda name, args: bad if name == "bad" else good

    assert h.updater.run_cycle() is False

    assert good.zones[ZONE][("test", "A")]["values"] == ["1.2.3.4"]


def test_verification_mismatch_fails_the_cycle() -> None:
    h = Harness()
    h.verify_result = False

    assert h.updater.run_cycle() is False


def test_dry_run_changes_nothing_and_does_not_verify() -> None:
    h = Harness()
    h.updater.dry_run = True

    assert h.updater.run_cycle() is True

    assert h.stored == {}
    assert h.verified == []


def test_annotations_run_when_dns_fails() -> None:
    config = make_config(
        kubernetes={"enabled": True, "label_selector": "a=b", "interfaces": ["wan1", "wan2"]}
    )
    h = Harness(config)
    h.provider.fail_apply = RuntimeError("API down")

    assert h.updater.run_cycle() is False

    assert h.annotations == [
        {"ips": ["1.2.3.4", "5.6.7.8"], "label_selector": "a=b", "dry_run": False}
    ]


def test_annotation_failure_fails_the_cycle() -> None:
    config = make_config(
        kubernetes={"enabled": True, "label_selector": "a=b", "interfaces": ["wan1"]}
    )
    h = Harness(config)
    h.annotate_error = RuntimeError("apiserver down")

    assert h.updater.run_cycle() is False
    assert h.stored[("test", "A")]["values"] == ["1.2.3.4"]  # DNS was still written


def test_annotations_are_off_by_default() -> None:
    h = Harness()
    h.updater.run_cycle()
    assert h.annotations == []


class TestExitCode:
    """`--once` reports the cycle's result as the process exit code."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, config_file: Path, ok: bool
    ) -> pytest.ExceptionInfo[SystemExit]:
        monkeypatch.setattr(sys, "argv", ["wan-dns-sync", "--once", "--config", str(config_file)])
        monkeypatch.setattr(main_module.Updater, "run_cycle", lambda self: ok)
        monkeypatch.setattr(main_module, "OPNsenseClient", lambda *a, **k: MagicMock())
        with pytest.raises(SystemExit) as exit_info:
            main_module.main()
        return exit_info

    def test_success_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, sample_config_yaml: Path
    ) -> None:
        assert self._run(monkeypatch, sample_config_yaml, ok=True).value.code == 0

    def test_failure_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, sample_config_yaml: Path
    ) -> None:
        assert self._run(monkeypatch, sample_config_yaml, ok=False).value.code == 1

    def test_bad_config_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad = tmp_path / "config.yaml"
        bad.write_text("opnsense: {}\n")
        assert self._run(monkeypatch, bad, ok=True).value.code == 1
