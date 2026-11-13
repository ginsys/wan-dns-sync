"""The real octoDNS deSEC and Hetzner providers, with only their API boundary faked.

A fake provider that reads its state fresh on every call cannot show a record
cache or a false "zone exists", so these cases run the real provider classes.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from wan_dns_sync.dns import DNSError, load_provider, sync_zone

ZONE = "example.com"


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.links: dict[str, Any] = {}

    def json(self) -> Any:
        return self._body


class FakeDesec:
    """Answers the deSEC API calls the provider makes, and records them."""

    def __init__(self, domains: dict[str, list[dict[str, Any]]]) -> None:
        self.domains = domains
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        data = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method.upper(), url, data))
        path = url.removeprefix("https://desec.io/api/v1/domains")
        if method.upper() == "GET" and path.startswith("/?"):
            return FakeResponse(200, [{"name": name} for name in self.domains])
        if method.upper() == "GET" and "/rrsets/" in path:
            name = path.split("/")[1]
            return FakeResponse(200, self.domains[name])
        if method.upper() == "PATCH":
            return FakeResponse(200, [])
        if method.upper() == "POST":
            return FakeResponse(
                201, {"keys": [{"dnskey": "k", "ds": ["d"]}], "name": data["name"]}
            )
        raise AssertionError(f"unexpected call {method} {url}")

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def desec(monkeypatch: pytest.MonkeyPatch) -> FakeDesec:
    api = FakeDesec({})
    monkeypatch.setattr(
        requests.Session, "request", lambda self, method, url, **kw: api.request(method, url, **kw)
    )
    return api


def _desec_provider() -> Any:
    return load_provider("desec", {"class": "octodns_desec.DesecProvider", "token": "t"})


def _rrset(subname: str, rtype: str, records: list[str], ttl: int = 3600) -> dict[str, Any]:
    return {"subname": subname, "type": rtype, "ttl": ttl, "records": records}


class TestDesec:
    def test_missing_zone_is_refused_and_nothing_is_created(self, desec: FakeDesec) -> None:
        desec.domains = {"other.com": []}

        with pytest.raises(DNSError, match="does not exist"):
            sync_zone(_desec_provider(), ZONE, {"office": ["1.2.3.4"]}, 3600, dry_run=False)

        assert desec.writes() == []

    def test_existing_empty_zone_gets_its_record_without_create_domain(
        self, desec: FakeDesec
    ) -> None:
        desec.domains = {ZONE: []}

        changed = sync_zone(_desec_provider(), ZONE, {"office": ["1.2.3.4"]}, 3600, dry_run=False)

        assert changed is True
        methods = [c[0] for c in desec.writes()]
        assert methods == ["PATCH"]  # no POST: create_domain was never called
        assert desec.writes()[0][2] == [_rrset("office", "A", ["1.2.3.4"])]

    def test_only_the_owned_record_is_written(self, desec: FakeDesec) -> None:
        desec.domains = {
            ZONE: [
                _rrset("office", "A", ["9.9.9.9"]),
                _rrset("office", "TXT", ['"keep"']),
                _rrset("www", "A", ["8.8.8.8"]),
                _rrset("", "NS", ["ns1.desec.io.", "ns2.desec.org."]),
            ]
        }

        sync_zone(_desec_provider(), ZONE, {"office": ["1.2.3.4"]}, 3600, dry_run=False)

        (patch,) = desec.writes()
        assert patch[2] == [_rrset("office", "A", ["1.2.3.4"])]

    def test_no_change_writes_nothing(self, desec: FakeDesec) -> None:
        desec.domains = {ZONE: [_rrset("office", "A", ["1.2.3.4"])]}

        changed = sync_zone(_desec_provider(), ZONE, {"office": ["1.2.3.4"]}, 3600, dry_run=False)

        assert changed is False
        assert desec.writes() == []

    def test_dry_run_reads_but_never_writes(self, desec: FakeDesec) -> None:
        desec.domains = {ZONE: [_rrset("office", "A", ["9.9.9.9"])]}

        changed = sync_zone(_desec_provider(), ZONE, {"office": ["1.2.3.4"]}, 3600, dry_run=True)

        assert changed is True
        assert desec.writes() == []


class FakeHcloud:
    """Stands in for the hcloud adapter the Hetzner provider talks to."""

    def __init__(self, zones: dict[str, list[dict[str, Any]]]) -> None:
        self.zones = zones
        self.record_reads = 0
        self.create_zone = MagicMock()
        self.upserts: list[Any] = []

    def domains(self) -> list[dict[str, Any]]:
        return [{"name": name} for name in self.zones]

    def zone_get(self, name: str) -> dict[str, Any]:
        if name not in self.zones:
            from octodns_hetzner.exceptions import HetznerClientNotFound

            raise HetznerClientNotFound()
        return {"id": f"id-{name}", "name": name, "ttl": 3600}

    def zone_records_get(self, zone_id: str) -> list[dict[str, Any]]:
        self.record_reads += 1
        return self.zones[zone_id.removeprefix("id-")]

    def zone_create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.create_zone(*args, **kwargs)  # type: ignore[no-any-return]

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("rrset_", "zone_record_")):
            return lambda *a, **k: self.upserts.append((name, a, k))
        raise AttributeError(name)


def _hetzner_provider(client: FakeHcloud) -> Any:
    provider = load_provider(
        "hetzner", {"class": "octodns_hetzner.HetznerProvider", "token": "t", "backend": "hcloud"}
    )
    provider._client = client
    return provider


def _hetzner_record(name: str, rtype: str, value: str) -> dict[str, Any]:
    return {"id": f"{name}-{rtype}", "zone_id": "id-" + ZONE, "name": name, "type": rtype,
            "value": value, "ttl": 300}  # fmt: skip


class TestHetzner:
    def test_missing_zone_is_refused_and_not_created(self) -> None:
        client = FakeHcloud({"other.com": []})

        with pytest.raises(DNSError, match="does not exist"):
            sync_zone(_hetzner_provider(client), ZONE, {"office": ["1.2.3.4"]}, 300, False)

        client.create_zone.assert_not_called()
        assert client.upserts == []

    def test_a_new_provider_instance_reads_the_zone_again(self) -> None:
        """The provider caches a zone's records, so one instance never sees drift."""
        client = FakeHcloud({ZONE: [_hetzner_record("office", "A", "1.2.3.4")]})

        sync_zone(_hetzner_provider(client), ZONE, {"office": ["1.2.3.4"]}, 300, True)
        sync_zone(_hetzner_provider(client), ZONE, {"office": ["1.2.3.4"]}, 300, True)

        assert client.record_reads == 2

    def test_dry_run_changes_nothing(self) -> None:
        client = FakeHcloud({ZONE: [_hetzner_record("office", "A", "9.9.9.9")]})

        changed = sync_zone(_hetzner_provider(client), ZONE, {"office": ["1.2.3.4"]}, 300, True)

        assert changed is True
        assert client.upserts == []
