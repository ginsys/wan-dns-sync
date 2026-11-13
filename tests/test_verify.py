"""Tests for DNS verification."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import dns.resolver

from wan_dns_sync.verify import (
    resolve_nameserver_ips,
    verify_a_records,
)


class TestResolveNameserverIps:
    """Tests for resolve_nameserver_ips."""

    def test_resolve_success(self) -> None:
        """Test successful nameserver resolution."""
        mock_answer = MagicMock()
        mock_rdata1 = MagicMock()
        mock_rdata1.address = "1.2.3.4"
        mock_rdata2 = MagicMock()
        mock_rdata2.address = "5.6.7.8"
        mock_answer.__iter__ = lambda self: iter([mock_rdata1, mock_rdata2])

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve.return_value = mock_answer

            ips = resolve_nameserver_ips()

        # Should have IPs for each nameserver
        assert len(ips) > 0

    def test_resolve_partial_failure(self) -> None:
        """Test resolution when some nameservers fail."""
        call_count = 0

        def mock_resolve(name: str, rdtype: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise dns.resolver.NXDOMAIN()
            mock_answer = MagicMock()
            mock_rdata = MagicMock()
            mock_rdata.address = "1.2.3.4"
            mock_answer.__iter__ = lambda self: iter([mock_rdata])
            return mock_answer

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve = mock_resolve

            ips = resolve_nameserver_ips()

        # Should still get IPs from successful resolutions
        assert len(ips) > 0

    def test_resolve_all_fail(self) -> None:
        """Test resolution when all nameservers fail."""
        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve.side_effect = dns.resolver.NXDOMAIN()

            ips = resolve_nameserver_ips()

        assert ips == []


class TestVerifyARecords:
    """Tests for verify_a_records."""

    def test_verify_match(self) -> None:
        """Test verification when DNS matches expected."""
        mock_ns_answer = MagicMock()
        mock_ns_rdata = MagicMock()
        mock_ns_rdata.address = "1.2.3.4"
        mock_ns_answer.__iter__ = lambda self: iter([mock_ns_rdata])

        mock_a_answer = MagicMock()
        mock_a_rdata = MagicMock()
        mock_a_rdata.address = "10.0.0.1"
        mock_a_answer.__iter__ = lambda self: iter([mock_a_rdata])

        def mock_resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "A" and "ns.hetzner" in name:
                return mock_ns_answer
            return mock_a_answer

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve = mock_resolve

            result = verify_a_records("test", "example.com", {"10.0.0.1"})

        assert result is True

    def test_verify_mismatch(self) -> None:
        """Test verification when DNS doesn't match expected."""
        mock_ns_answer = MagicMock()
        mock_ns_rdata = MagicMock()
        mock_ns_rdata.address = "1.2.3.4"
        mock_ns_answer.__iter__ = lambda self: iter([mock_ns_rdata])

        mock_a_answer = MagicMock()
        mock_a_rdata = MagicMock()
        mock_a_rdata.address = "10.0.0.1"
        mock_a_answer.__iter__ = lambda self: iter([mock_a_rdata])

        def mock_resolve(name: str, rdtype: str) -> MagicMock:
            if rdtype == "A" and "ns.hetzner" in name:
                return mock_ns_answer
            return mock_a_answer

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve = mock_resolve

            result = verify_a_records("test", "example.com", {"10.0.0.2"})

        assert result is False

    def test_verify_nxdomain_expected(self) -> None:
        """Test verification when NXDOMAIN is expected (empty IPs)."""
        mock_ns_answer = MagicMock()
        mock_ns_rdata = MagicMock()
        mock_ns_rdata.address = "1.2.3.4"
        mock_ns_answer.__iter__ = lambda self: iter([mock_ns_rdata])

        call_count = 0

        def mock_resolve(name: str, rdtype: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "ns.hetzner" in name:
                return mock_ns_answer
            raise dns.resolver.NXDOMAIN()

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve = mock_resolve

            result = verify_a_records("test", "example.com", set())

        assert result is True

    def test_verify_nxdomain_unexpected(self) -> None:
        """Test verification when NXDOMAIN is unexpected."""
        mock_ns_answer = MagicMock()
        mock_ns_rdata = MagicMock()
        mock_ns_rdata.address = "1.2.3.4"
        mock_ns_answer.__iter__ = lambda self: iter([mock_ns_rdata])

        def mock_resolve(name: str, rdtype: str) -> MagicMock:
            if "ns.hetzner" in name:
                return mock_ns_answer
            raise dns.resolver.NXDOMAIN()

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve = mock_resolve

            result = verify_a_records("test", "example.com", {"10.0.0.1"})

        assert result is False

    def test_verify_no_nameservers(self) -> None:
        """Test verification when nameserver resolution fails."""
        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve.side_effect = Exception("DNS error")

            result = verify_a_records("test", "example.com", {"10.0.0.1"})

        assert result is False

    def test_verify_multiple_ips(self) -> None:
        """Test verification with multiple IPs."""
        mock_ns_answer = MagicMock()
        mock_ns_rdata = MagicMock()
        mock_ns_rdata.address = "1.2.3.4"
        mock_ns_answer.__iter__ = lambda self: iter([mock_ns_rdata])

        mock_a_answer = MagicMock()
        mock_a_rdata1 = MagicMock()
        mock_a_rdata1.address = "10.0.0.1"
        mock_a_rdata2 = MagicMock()
        mock_a_rdata2.address = "10.0.0.2"
        mock_a_answer.__iter__ = lambda self: iter([mock_a_rdata1, mock_a_rdata2])

        def mock_resolve(name: str, rdtype: str) -> MagicMock:
            if "ns.hetzner" in name:
                return mock_ns_answer
            return mock_a_answer

        with patch("dns.resolver.Resolver") as mock_resolver:
            mock_resolver.return_value.resolve = mock_resolve

            result = verify_a_records("test", "example.com", {"10.0.0.1", "10.0.0.2"})

        assert result is True
