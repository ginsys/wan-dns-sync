"""Tests for DNS verification against a zone's own nameservers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import dns.resolver
import pytest

from wan_dns_sync import verify
from wan_dns_sync.verify import verify_a_records, zone_nameservers


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


class TestZoneNameservers:
    def test_resolves_every_ns_of_the_zone(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("ns1.example.net."), _ns("ns2.example.net."))
            if rdtype == "AAAA":
                raise dns.resolver.NoAnswer()
            addresses = {"ns1.example.net.": "1.1.1.1", "ns2.example.net.": "2.2.2.2"}
            return _answer(_rdata(addresses[name]))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {
                "ns1.example.net.": ["1.1.1.1"],
                "ns2.example.net.": ["2.2.2.2"],
            }

    def test_ipv6_only_nameserver_is_kept(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("v6.example.net."))
            if rdtype == "A":
                raise dns.resolver.NoAnswer()
            return _answer(_rdata("2001:db8::1"))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {"v6.example.net.": ["2001:db8::1"]}

    def test_a_failed_address_lookup_is_not_an_absent_address(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("ns1.example.net."))
            if rdtype == "AAAA":
                raise dns.resolver.LifetimeTimeout()
            return _answer(_rdata("1.1.1.1"))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {"ns1.example.net.": None}

    def test_ns_lookup_failure_returns_nothing(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = dns.resolver.NXDOMAIN()
            assert zone_nameservers("example.com") == {}

    def test_unresolvable_nameserver_is_kept_with_no_address(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("bad.example.net."), _ns("ns2.example.net."))
            if name == "bad.example.net.":
                raise dns.resolver.NXDOMAIN()
            if rdtype == "AAAA":
                raise dns.resolver.NoAnswer()
            return _answer(_rdata("2.2.2.2"))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {
                "bad.example.net.": [],
                "ns2.example.net.": ["2.2.2.2"],
            }


def _no_nameservers(response: object) -> dns.resolver.NoNameservers:
    """What dnspython raises when its one server failed: errors carry the response, if any."""
    return dns.resolver.NoNameservers(
        request=MagicMock(), errors=[("2001:db8::1", False, 53, "failed", response)]
    )


class TestAnswers:
    def test_an_unreachable_server_is_unreachable(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = _no_nameservers(None)
            with pytest.raises(verify.UnreachableError):
                verify._answers("2001:db8::1", "a.example.com", 1.0)

    def test_a_server_that_answered_with_a_failure_is_not_unreachable(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = _no_nameservers(MagicMock())
            with pytest.raises(dns.resolver.NoNameservers):
                verify._answers("2001:db8::1", "a.example.com", 1.0)


class TestVerifyARecords:
    def test_a_servfail_from_one_address_fails_even_when_another_answers(self) -> None:
        def answers(ns: str, fqdn: str, timeout: float) -> set[str]:
            if ":" in ns:
                raise _no_nameservers(MagicMock())
            return {"10.0.0.1"}

        servers = {"ns1.": ["1.1.1.1", "2001:db8::1"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", side_effect=answers),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_all_nameservers_match(self) -> None:
        servers = {"ns1.": ["1.1.1.1"], "ns2.": ["2.2.2.2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is True

    def test_one_nameserver_disagrees(self) -> None:
        answers = {"1.1.1.1": {"10.0.0.1"}, "2.2.2.2": {"10.0.0.9"}}
        servers = {"ns1.": ["1.1.1.1"], "ns2.": ["2.2.2.2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", side_effect=lambda ns, f, t: answers[ns]),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_a_nameserver_without_an_address_fails_even_when_the_others_match(self) -> None:
        servers = {"bad.": [], "ns2.": ["2.2.2.2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_a_nameserver_whose_lookup_failed_fails_even_when_the_others_match(self) -> None:
        servers = {"ns1.": None, "ns2.": ["2.2.2.2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_every_address_of_a_nameserver_is_checked(self) -> None:
        answers = {"2.2.2.2": {"10.0.0.1"}, "2001:db8::2": {"10.0.0.9"}}
        servers = {"ns2.": ["2.2.2.2", "2001:db8::2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", side_effect=lambda ns, f, t: answers[ns]),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_an_unreachable_address_is_no_verdict_when_another_answers(self) -> None:
        """An IPv4-only host cannot reach a nameserver's IPv6 address."""

        def answers(ns: str, fqdn: str, timeout: float) -> set[str]:
            if ":" in ns:
                raise OSError("Network is unreachable")
            return {"10.0.0.1"}

        servers = {"ns1.": ["1.1.1.1", "2001:db8::1"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", side_effect=answers),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is True

    def test_a_nameserver_none_of_whose_addresses_answers_fails(self) -> None:
        servers = {"ns1.": ["2001:db8::1"], "ns2.": ["2.2.2.2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(
                verify,
                "_answers",
                side_effect=lambda ns, f, t: (
                    (_ for _ in ()).throw(OSError("unreachable")) if ":" in ns else {"10.0.0.1"}
                ),
            ),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_resolver_without_configuration_fails_instead_of_raising(self) -> None:
        with patch("dns.resolver.Resolver", side_effect=dns.resolver.NoResolverConfiguration()):
            assert verify.zone_nameservers("example.com") == {}
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_nameserver_error_fails(self) -> None:
        with (
            patch.object(verify, "zone_nameservers", return_value={"ns1.": ["1.1.1.1"]}),
            patch.object(verify, "_answers", side_effect=TimeoutError("slow")),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_no_nameservers_fails(self) -> None:
        with patch.object(verify, "zone_nameservers", return_value={}):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_queries_the_full_name(self) -> None:
        with (
            patch.object(verify, "zone_nameservers", return_value={"ns1.": ["1.1.1.1"]}),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}) as answers,
        ):
            verify_a_records("test", "example.com", {"10.0.0.1"})
        assert answers.call_args.args[1] == "test.example.com"
