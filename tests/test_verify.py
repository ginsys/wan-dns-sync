"""Tests for DNS verification against a zone's own nameservers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import dns.flags
import dns.resolver
import pytest

from wan_dns_sync import verify
from wan_dns_sync.verify import verify_a_records, zone_nameservers


def _answer(*items: object, flags: int = 0) -> MagicMock:
    answer = MagicMock()
    answer.__iter__ = lambda self: iter(items)
    answer.response.flags = flags
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
    def test_resolves_the_ipv4_address_of_every_ns(self) -> None:
        asked: list[str] = []

        def resolve(name: str, rdtype: str) -> MagicMock:
            asked.append(rdtype)
            if rdtype == "NS":
                return _answer(_ns("ns1.example.net."), _ns("ns2.example.net."))
            addresses = {"ns1.example.net.": "1.1.1.1", "ns2.example.net.": "2.2.2.2"}
            return _answer(_rdata(addresses[name]))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {
                "ns1.example.net.": ["1.1.1.1"],
                "ns2.example.net.": ["2.2.2.2"],
            }
        assert "AAAA" not in asked  # an IPv4-only host cannot use an IPv6 address

    def test_a_failed_address_lookup_is_not_an_absent_address(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("ns1.example.net."))
            raise dns.resolver.LifetimeTimeout()

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {"ns1.example.net.": None}

    def test_ns_lookup_failure_returns_nothing(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = dns.resolver.NXDOMAIN()
            assert zone_nameservers("example.com") == {}

    def test_resolver_without_configuration_returns_nothing(self) -> None:
        with patch("dns.resolver.Resolver", side_effect=dns.resolver.NoResolverConfiguration()):
            assert zone_nameservers("example.com") == {}

    def test_a_truncated_ns_answer_returns_nothing(self) -> None:
        """A partial NS list would silently leave servers out of the verification."""
        truncated = _answer(_ns("ns1.example.net."), flags=int(dns.flags.TC))
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.return_value = truncated
            assert zone_nameservers("example.com") == {}

    def test_a_truncated_address_answer_is_a_failed_lookup(self) -> None:
        """A partial address list would silently leave addresses out of the verification."""

        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("ns1.example.net."))
            return _answer(_rdata("1.1.1.1"), flags=int(dns.flags.TC))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {"ns1.example.net.": None}

    def test_nameserver_without_an_ipv4_address_is_kept_with_none(self) -> None:
        def resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "NS":
                return _answer(_ns("bad.example.net."), _ns("ns2.example.net."))
            if name == "bad.example.net.":
                raise dns.resolver.NoAnswer()
            return _answer(_rdata("2.2.2.2"))

        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve = resolve
            assert zone_nameservers("example.com") == {
                "bad.example.net.": [],
                "ns2.example.net.": ["2.2.2.2"],
            }


class TestAnswers:
    def _resolver_returning(self, resolver: MagicMock, addresses: list[str], flags: int) -> None:
        answer = _answer(*(_rdata(a) for a in addresses))
        answer.response.flags = flags
        resolver.return_value.resolve.return_value = answer

    def test_the_a_records_of_a_complete_answer(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            self._resolver_returning(resolver, ["10.0.0.1"], 0)
            assert verify._answers("1.1.1.1", "a.example.com", 1.0) == {"10.0.0.1"}

    def test_a_truncated_answer_is_a_failure_even_when_the_records_match(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            self._resolver_returning(resolver, ["10.0.0.1"], int(dns.flags.TC))
            with pytest.raises(ValueError, match="truncated"):
                verify._answers("1.1.1.1", "a.example.com", 1.0)

    def test_no_answer_is_an_empty_set(self) -> None:
        with patch("dns.resolver.Resolver") as resolver:
            resolver.return_value.resolve.side_effect = dns.resolver.NoAnswer()
            assert verify._answers("1.1.1.1", "a.example.com", 1.0) == set()


class TestVerifyARecords:
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
        answers = {"2.2.2.2": {"10.0.0.1"}, "3.3.3.3": {"10.0.0.9"}}
        servers = {"ns2.": ["2.2.2.2", "3.3.3.3"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", side_effect=lambda ns, f, t: answers[ns]),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("slow"),
            OSError("Network is unreachable"),
            dns.resolver.LifetimeTimeout(),  # what a discarded malformed reply ends as
            dns.resolver.NoNameservers(request=MagicMock(), errors=[]),
            ValueError("the answer is truncated"),
        ],
    )
    def test_any_error_from_any_address_fails(self, error: Exception) -> None:
        answers = {"1.1.1.1": {"10.0.0.1"}}

        def answer(ns: str, fqdn: str, timeout: float) -> set[str]:
            if ns == "2.2.2.2":
                raise error
            return answers[ns]

        servers = {"ns1.": ["1.1.1.1", "2.2.2.2"]}
        with (
            patch.object(verify, "zone_nameservers", return_value=servers),
            patch.object(verify, "_answers", side_effect=answer),
        ):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_no_nameservers_fails(self) -> None:
        with patch.object(verify, "zone_nameservers", return_value={}):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_resolver_without_configuration_fails_instead_of_raising(self) -> None:
        with patch("dns.resolver.Resolver", side_effect=dns.resolver.NoResolverConfiguration()):
            assert verify_a_records("test", "example.com", {"10.0.0.1"}) is False

    def test_queries_the_full_name(self) -> None:
        with (
            patch.object(verify, "zone_nameservers", return_value={"ns1.": ["1.1.1.1"]}),
            patch.object(verify, "_answers", return_value={"10.0.0.1"}) as answers,
        ):
            verify_a_records("test", "example.com", {"10.0.0.1"})
        assert answers.call_args.args[1] == "test.example.com"
