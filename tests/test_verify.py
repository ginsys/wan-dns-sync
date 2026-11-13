"""Tests for DNS verification against a zone's own nameservers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import dns.resolver

from wan_dns_sync import verify
from wan_dns_sync.verify import verify_a_records, zone_nameserver_ips


def _answer(*items: object) -> MagicMock:
    answer = MagicMock()
    answer.__iter__ = lambda self: iter(items)
    return answer


def _rdata(address: str) -> MagicMock:
    rdata = MagicMock()
    rdata.address = address
    return rdata


def _ns(target: str) -> MagicMock:
    rdata = MagicMock()
    rdata.target = target
    return rdata


class TestZoneNameserverIps:
    def test_resolves_every_ns_of_the_zone(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("ns1.example.net."), _ns("ns2.example.net."))
            addresses = {"ns1.example.net.": "1.1.1.1", "ns2.example.net.": "2.2.2.2"}
            return _answer(_rdata(addresses[name]))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameserver_ips("example.com") == ["1.1.1.1", "2.2.2.2"]

    def test_ns_lookup_failure_returns_nothing(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = dns.resolver.NXDOMAIN()
            assert zone_nameserver_ips("example.com") == []

    def test_one_unresolvable_nameserver_is_skipped(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("bad.example.net."), _ns("ns2.example.net."))
            if name == "bad.example.net.":
                raise dns.resolver.NXDOMAIN()
            return _answer(_rdata("2.2.2.2"))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameserver_ips("example.com") == ["2.2.2.2"]


class TestVerifyARecords:
    def test_all_nameservers_match(self) -> None:
        with (
            patch.object(verify, "zone_nameserver_ips", return_value=["1.1.1.1", "2.2.2.2"]),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is True

    def test_one_nameserver_disagrees(self) -> None:
        answers = {"1.1.1.1": {"10.0.0.1"}, "2.2.2.2": {"10.0.0.9"}}
        with (
            patch.object(verify, "zone_nameserver_ips", return_value=["1.1.1.1", "2.2.2.2"]),
            patch.object(verify, "_answers", side_effect=lambda ns, f, t: answers[ns]),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_nameserver_error_fails(self) -> None:
        with (
            patch.object(verify, "zone_nameserver_ips", return_value=["1.1.1.1"]),
            patch.object(verify, "_answers", side_effect=TimeoutError("slow")),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_no_nameservers_fails(self) -> None:
        with patch.object(verify, "zone_nameserver_ips", return_value=[]):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_queries_the_full_name(self) -> None:
        with (
            patch.object(verify, "zone_nameserver_ips", return_value=["1.1.1.1"]),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}) as answers,
        ):
            verify_a_records("test", "example.com", {"10.0.0.1"})
        assert answers.call_args.args[1] == "test.example.com"
