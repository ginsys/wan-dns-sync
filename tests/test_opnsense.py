"""Tests for OPNsense client."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from wan_dns_sync.config import OPNsenseConfig
from wan_dns_sync.opnsense import OPNsenseClient


@pytest.fixture
def opnsense_config() -> OPNsenseConfig:
    """Create a sample OPNsense configuration."""
    return OPNsenseConfig(
        url="https://opnsense.local/api",
        key="test-key",
        secret="test-secret",
        verify_ssl=True,
        interfaces={"wan1": "wan", "wan2": "opt1"},
    )


class TestOPNsenseClient:
    """Tests for OPNsenseClient."""

    @respx.mock
    def test_get_interface_ips_success(
        self,
        opnsense_config: OPNsenseConfig,
        mock_opnsense_response: dict[str, Any],
    ) -> None:
        """Test successful IP retrieval."""
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(200, json=mock_opnsense_response)
        )

        with OPNsenseClient(opnsense_config) as client:
            ips = client.get_interface_ips()

        assert ips == {"wan1": "1.2.3.4", "wan2": "5.6.7.8"}

    @respx.mock
    def test_get_interface_ips_missing_interface(
        self,
        opnsense_config: OPNsenseConfig,
    ) -> None:
        """Test handling of missing interface."""
        response = {
            "wan": {"ipv4": [{"ipaddr": "1.2.3.4"}]},
            # opt1 is missing
        }
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(200, json=response)
        )

        with OPNsenseClient(opnsense_config) as client:
            ips = client.get_interface_ips()

        # Only wan1 should be returned, wan2 is missing
        assert ips == {"wan1": "1.2.3.4"}

    @respx.mock
    def test_get_interface_ips_no_ipv4(
        self,
        opnsense_config: OPNsenseConfig,
    ) -> None:
        """Test handling of interface without IPv4 address."""
        response = {
            "wan": {"ipv4": []},  # No IPv4 addresses
            "opt1": {"ipv4": [{"ipaddr": "5.6.7.8"}]},
        }
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(200, json=response)
        )

        with OPNsenseClient(opnsense_config) as client:
            ips = client.get_interface_ips()

        # Only wan2 should be returned
        assert ips == {"wan2": "5.6.7.8"}

    @respx.mock
    def test_get_interface_ips_fallback_ipaddr(
        self,
        opnsense_config: OPNsenseConfig,
    ) -> None:
        """Test fallback to direct ipaddr field."""
        response = {
            "wan": {"ipaddr": "1.2.3.4"},  # Direct ipaddr instead of ipv4 array
            "opt1": {"ipv4": [{"ipaddr": "5.6.7.8"}]},
        }
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(200, json=response)
        )

        with OPNsenseClient(opnsense_config) as client:
            ips = client.get_interface_ips()

        assert ips == {"wan1": "1.2.3.4", "wan2": "5.6.7.8"}

    @respx.mock
    def test_get_interface_ips_api_error(
        self,
        opnsense_config: OPNsenseConfig,
    ) -> None:
        """Test handling of API error."""
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        with (
            OPNsenseClient(opnsense_config) as client,
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.get_interface_ips()

    @pytest.mark.parametrize(
        "address",
        ["10.0.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1", "127.0.0.1", "not-an-ip", "::1"],
    )
    @respx.mock
    def test_non_public_address_is_skipped(
        self, opnsense_config: OPNsenseConfig, address: str
    ) -> None:
        response = {"wan": {"ipv4": [{"ipaddr": address}]}, "opt1": {"ipaddr": "5.6.7.8"}}
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(200, json=response)
        )

        with OPNsenseClient(opnsense_config) as client:
            assert client.get_interface_ips() == {"wan2": "5.6.7.8"}

    @respx.mock
    def test_private_address_allowed_by_setting(self, opnsense_config: OPNsenseConfig) -> None:
        response = {"wan": {"ipv4": [{"ipaddr": "10.0.0.1"}]}}
        respx.get("https://opnsense.local/api/diagnostics/interface/getInterfaceConfig").mock(
            return_value=httpx.Response(200, json=response)
        )

        with OPNsenseClient(opnsense_config, allow_private_ips=True) as client:
            assert client.get_interface_ips() == {"wan1": "10.0.0.1"}

    def test_ca_file_is_used_for_verification(self, tmp_path: Path) -> None:
        ca = tmp_path / "ca.pem"
        config = OPNsenseConfig(
            url="https://opnsense.local",
            key="key",
            secret="secret",
            ca_file=ca,
            interfaces={"wan": "wan"},
        )
        with patch("httpx.Client") as mock_client:
            OPNsenseClient(config)
        assert mock_client.call_args.kwargs["verify"] == str(ca)

    def test_verify_ssl_disabled(self) -> None:
        """Test that verify_ssl=False is passed to httpx client."""
        config = OPNsenseConfig(
            url="https://opnsense.local/api",
            key="key",
            secret="secret",
            verify_ssl=False,
            interfaces={"wan": "wan"},
        )

        with patch("httpx.Client") as mock_client:
            OPNsenseClient(config)
            mock_client.assert_called_once()
            call_kwargs = mock_client.call_args.kwargs
            assert call_kwargs["verify"] is False

    def test_base_url_adds_api_suffix(self) -> None:
        """Test that /api is appended when missing in URL."""
        config = OPNsenseConfig(
            url="https://opnsense.local",
            key="key",
            secret="secret",
            verify_ssl=True,
            interfaces={"wan": "wan"},
        )

        with patch("httpx.Client"):
            client = OPNsenseClient(config)

        assert client.base_url == "https://opnsense.local/api"

    def test_context_manager(self, opnsense_config: OPNsenseConfig) -> None:
        """Test context manager properly closes client."""
        with patch("httpx.Client") as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value = mock_instance

            with OPNsenseClient(opnsense_config):
                pass

            mock_instance.close.assert_called_once()
