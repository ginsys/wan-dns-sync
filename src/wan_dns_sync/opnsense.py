"""OPNsense API client for retrieving WAN interface IP addresses."""

import ipaddress

import httpx
import structlog

from .config import OPNsenseConfig

logger = structlog.get_logger()


def _publishable(address: str, allow_private: bool) -> bool:
    """True for a plain IPv4 address that may be published."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if parsed.is_multicast or parsed.is_unspecified or parsed.is_reserved or parsed.is_loopback:
        return False  # never a host address, whatever allow_private says
    return allow_private or parsed.is_global


class OPNsenseClient:
    """Client for OPNsense REST API."""

    def __init__(self, config: OPNsenseConfig, allow_private_ips: bool = False) -> None:
        self.config = config
        self.allow_private_ips = allow_private_ips
        base_url = config.url.rstrip("/")
        if not base_url.endswith("/api"):
            base_url = f"{base_url}/api"
        self.base_url = base_url
        verify: bool | str = str(config.ca_file) if config.ca_file else config.verify_ssl
        self._client = httpx.Client(
            auth=(config.key.get_secret_value(), config.secret.get_secret_value()),
            verify=verify,
            timeout=30.0,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> "OPNsenseClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_interface_ips(self) -> dict[str, str]:
        """
        Get IP addresses for configured interfaces.

        Returns:
            Dictionary mapping logical interface names to their IPv4 addresses.
        """
        # Query OPNsense for interface information
        # The diagnostics/interface/getInterfaceConfig endpoint returns detailed interface info
        url = f"{self.base_url}/diagnostics/interface/getInterfaceConfig"

        logger.debug("Querying OPNsense interface config", url=url)
        response = self._client.get(url)
        response.raise_for_status()

        interface_data = response.json()

        # Map logical names to IP addresses
        result: dict[str, str] = {}
        for logical_name, opnsense_name in self.config.interfaces.items():
            if opnsense_name not in interface_data:
                logger.warning(
                    "Interface not found in OPNsense",
                    logical_name=logical_name,
                    opnsense_name=opnsense_name,
                    available_interfaces=list(interface_data.keys()),
                )
                continue

            iface_info = interface_data[opnsense_name]

            # Extract IPv4 address - the structure varies, common fields to check
            ipv4_addr = None

            # Try 'ipv4' array first (common format)
            if "ipv4" in iface_info and isinstance(iface_info["ipv4"], list):
                for addr_info in iface_info["ipv4"]:
                    if "ipaddr" in addr_info:
                        ipv4_addr = addr_info["ipaddr"]
                        break

            # Fallback: direct 'ipaddr' field
            if ipv4_addr is None and "ipaddr" in iface_info:
                ipv4_addr = iface_info["ipaddr"]

            if ipv4_addr and not _publishable(str(ipv4_addr), self.allow_private_ips):
                logger.warning(
                    "Ignoring address that is not a public IPv4 address",
                    logical_name=logical_name,
                    opnsense_name=opnsense_name,
                    address=ipv4_addr,
                )
            elif ipv4_addr:
                result[logical_name] = ipv4_addr
                logger.debug(
                    "Found interface IP",
                    logical_name=logical_name,
                    opnsense_name=opnsense_name,
                    ip=ipv4_addr,
                )
            else:
                logger.warning(
                    "No IPv4 address found for interface",
                    logical_name=logical_name,
                    opnsense_name=opnsense_name,
                )

        return result
