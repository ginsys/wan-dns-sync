"""Tests for the octoDNS-backed zone sync."""

from __future__ import annotations

import pytest

from tests.fakes import FakeProvider
from wan_dns_sync.dns import DNSError, load_provider, sync_zone

ZONE = "example.com"


def _zone_with_extras() -> dict[tuple[str, str], dict[str, object]]:
    return {
        ("office", "A"): {"ttl": 300, "values": ["9.9.9.9"]},
        ("office", "TXT"): {"ttl": 300, "values": ["keep me"]},
        ("", "MX"): {"ttl": 300, "values": [{"exchange": "mx.example.com.", "preference": 10}]},
        ("www", "A"): {"ttl": 300, "values": ["8.8.8.8"]},
    }


class TestOwnership:
    def test_only_owned_records_change(self) -> None:
        provider = FakeProvider("p", {ZONE: _zone_with_extras()})

        changed = sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=False)

        assert changed is True
        stored = provider.zones[ZONE]
        assert stored[("office", "A")]["values"] == ["1.2.3.4"]
        assert stored[("office", "TXT")]["values"] == ["keep me"]
        assert ("", "MX") in stored
        assert stored[("www", "A")]["values"] == ["8.8.8.8"]

    def test_unowned_records_are_never_deleted(self) -> None:
        provider = FakeProvider("p", {ZONE: _zone_with_extras()})

        sync_zone(provider, ZONE, {"new": ["1.2.3.4"]}, 300, dry_run=False)

        stored = provider.zones[ZONE]
        assert ("www", "A") in stored
        assert ("office", "A") in stored  # not owned this cycle, so left alone
        assert stored[("new", "A")]["values"] == ["1.2.3.4"]

    def test_multiple_addresses(self) -> None:
        provider = FakeProvider("p", {ZONE: {}})

        sync_zone(provider, ZONE, {"multi": ["5.6.7.8", "1.2.3.4"]}, 300, dry_run=False)

        assert provider.zones[ZONE][("multi", "A")]["values"] == ["1.2.3.4", "5.6.7.8"]


class TestChanges:
    def test_no_change_applies_nothing(self) -> None:
        provider = FakeProvider("p", {ZONE: {("office", "A"): {"ttl": 300, "values": ["1.2.3.4"]}}})

        changed = sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=False)

        assert changed is False
        assert provider.applied == []

    def test_ttl_change_alone_is_applied(self) -> None:
        old = {("office", "A"): {"ttl": 3600, "values": ["1.2.3.4"]}}
        provider = FakeProvider("p", {ZONE: old})

        changed = sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=False)

        assert changed is True
        assert provider.zones[ZONE][("office", "A")]["ttl"] == 300

    def test_dry_run_plans_but_does_not_apply(self) -> None:
        provider = FakeProvider("p", {ZONE: _zone_with_extras()})

        changed = sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=True)

        assert changed is True
        assert provider.applied == []
        assert provider.zones[ZONE][("office", "A")]["values"] == ["9.9.9.9"]

    def test_apply_marks_the_plan_as_existing(self) -> None:
        provider = FakeProvider("p", {ZONE: {}})  # an existing zone with no records

        sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=False)

        assert provider.applied[0].exists is True


class TestMissingZone:
    def test_missing_zone_is_refused_and_nothing_is_written(self) -> None:
        provider = FakeProvider("p", {"other.com": {}})

        with pytest.raises(DNSError, match="does not exist"):
            sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=False)

        assert provider.applied == []
        assert ZONE not in provider.zones

    def test_trailing_dot_is_ignored_when_matching_zones(self) -> None:
        provider = FakeProvider("p", {ZONE + ".": {}})

        assert sync_zone(provider, ZONE, {"office": ["1.2.3.4"]}, 300, dry_run=True) is True


class TestLoadProvider:
    def test_loads_a_provider_class(self) -> None:
        provider = load_provider("fake", {"class": "tests.fakes.FakeProvider"})
        assert isinstance(provider, FakeProvider)
        assert provider.id == "fake"

    def test_passes_the_remaining_arguments(self) -> None:
        provider = load_provider("fake", {"class": "tests.fakes.FakeProvider", "zones": {ZONE: {}}})
        assert isinstance(provider, FakeProvider)
        assert provider.zones == {ZONE: {}}

    def test_rejects_a_class_that_is_not_a_provider(self) -> None:
        with pytest.raises(DNSError, match="not an octoDNS provider"):
            load_provider("x", {"class": "os.path.join"})

    def test_rejects_a_bare_name(self) -> None:
        with pytest.raises(DNSError, match="dotted"):
            load_provider("x", {"class": "FakeProvider"})
