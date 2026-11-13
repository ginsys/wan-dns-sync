"""Tests for Hetzner DNS client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from wan_dns_sync.config import HetznerConfig
from wan_dns_sync.hetzner import HetznerAPIError, HetznerDNSClient


@pytest.fixture
def hetzner_config() -> HetznerConfig:
    """Create a sample Hetzner configuration."""
    return HetznerConfig(
        token="test-token",
        zone="example.com",
        ttl=300,
    )


@pytest.fixture
def mock_zones_client() -> MagicMock:
    """Create a mock zones client."""
    zones = MagicMock()

    # Mock zone
    zone = MagicMock()
    zone.id = "zone-123"
    zone.name = "example.com"
    zones.get_all.return_value = [zone]

    return zones


class TestHetznerDNSClient:
    """Tests for HetznerDNSClient."""

    def test_get_zone_success(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
    ) -> None:
        """Test successful zone retrieval."""
        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                zone = client._get_zone()

            assert zone.name == "example.com"
            assert zone.id == "zone-123"

    def test_get_zone_not_found(
        self,
        hetzner_config: HetznerConfig,
    ) -> None:
        """Test zone not found error."""
        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones.get_all.return_value = []

            with (
                HetznerDNSClient(hetzner_config) as client,
                pytest.raises(ValueError, match="not found"),
            ):
                client._get_zone()

    def test_get_zone_cached(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
    ) -> None:
        """Test that zone is cached after first retrieval."""
        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                client._get_zone()
                client._get_zone()

            # Should only call get_all once
            assert mock_zones_client.get_all.call_count == 1

    def test_get_a_record_ips_exists(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
        mock_hcloud_rrset: MagicMock,
    ) -> None:
        """Test getting existing A record IPs."""
        mock_zones_client.get_rrset_all.return_value = [mock_hcloud_rrset]

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                ips = client.get_a_record_ips("test")

            assert ips == {"1.2.3.4"}

    def test_get_a_record_ips_not_exists(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
    ) -> None:
        """Test getting non-existent A record IPs."""
        mock_zones_client.get_rrset_all.return_value = []

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                ips = client.get_a_record_ips("nonexistent")

            assert ips == set()

    def test_sync_a_records_no_change(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
        mock_hcloud_rrset: MagicMock,
    ) -> None:
        """Test sync when no changes needed."""
        mock_zones_client.get_rrset_all.return_value = [mock_hcloud_rrset]

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                changed = client.sync_a_records("test", ["1.2.3.4"])

            assert changed is False
            mock_zones_client.set_rrset_records.assert_not_called()
            mock_zones_client.create_rrset.assert_not_called()

    def test_sync_a_records_update(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
        mock_hcloud_rrset: MagicMock,
    ) -> None:
        """Test sync when update needed."""
        mock_zones_client.get_rrset_all.return_value = [mock_hcloud_rrset]

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                changed = client.sync_a_records("test", ["9.9.9.9"])

            assert changed is True
            mock_zones_client.set_rrset_records.assert_called_once()

    def test_sync_a_records_create(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
    ) -> None:
        """Test sync when create needed."""
        mock_zones_client.get_rrset_all.return_value = []

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                changed = client.sync_a_records("new", ["1.2.3.4"])

            assert changed is True
            mock_zones_client.create_rrset.assert_called_once()

    def test_sync_a_records_delete(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
        mock_hcloud_rrset: MagicMock,
    ) -> None:
        """Test sync when delete needed (empty IPs)."""
        mock_zones_client.get_rrset_all.return_value = [mock_hcloud_rrset]

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                changed = client.sync_a_records("test", [])

            assert changed is True
            mock_zones_client.delete_rrset.assert_called_once()

    def test_sync_a_records_dry_run(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
        mock_hcloud_rrset: MagicMock,
    ) -> None:
        """Test sync in dry run mode."""
        mock_zones_client.get_rrset_all.return_value = [mock_hcloud_rrset]

        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                changed = client.sync_a_records("test", ["9.9.9.9"], dry_run=True)

            assert changed is True
            # No actual changes should be made
            mock_zones_client.set_rrset_records.assert_not_called()
            mock_zones_client.create_rrset.assert_not_called()
            mock_zones_client.delete_rrset.assert_not_called()

    def test_health_check_success(
        self,
        hetzner_config: HetznerConfig,
        mock_zones_client: MagicMock,
    ) -> None:
        """Test successful health check."""
        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones = mock_zones_client

            with HetznerDNSClient(hetzner_config) as client:
                assert client.health_check() is True

    def test_health_check_failure(
        self,
        hetzner_config: HetznerConfig,
    ) -> None:
        """Test failed health check."""
        with patch("wan_dns_sync.hetzner.HCloudClient") as mock_hcloud:
            mock_hcloud.return_value.zones.get_all.side_effect = Exception("API Error")

            with HetznerDNSClient(hetzner_config) as client:
                assert client.health_check() is False


class TestHetznerAPIError:
    """Tests for HetznerAPIError."""

    def test_error_with_int_status_code(self) -> None:
        """Test error with integer status code."""
        error = HetznerAPIError("test error", status_code=429)
        assert error.status_code == 429

    def test_error_with_str_status_code(self) -> None:
        """Test error with string status code (normalized to int)."""
        error = HetznerAPIError("test error", status_code="500")
        assert error.status_code == 500

    def test_error_with_invalid_str_status_code(self) -> None:
        """Test error with invalid string status code."""
        error = HetznerAPIError("test error", status_code="invalid")
        assert error.status_code is None

    def test_error_with_none_status_code(self) -> None:
        """Test error with no status code."""
        error = HetznerAPIError("test error")
        assert error.status_code is None
