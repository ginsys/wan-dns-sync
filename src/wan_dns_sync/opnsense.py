"""OPNsense API client for retrieving WAN interface IP addresses."""

import httpx
import structlog

from .config import OPNsenseConfig

logger = structlog.get_logger()


class OPNsenseClient:
    """Client for OPNsense REST API."""

    def __init__(self, config: OPNsenseConfig) -> None:
        self.config = config
        base_url = config.url.rstrip("/")
        if not base_url.endswith("/api"):
            base_url = f"{base_url}/api"
        self.base_url = base_url
        self._client = httpx.Client(
            auth=(config.key, config.secret),
            verify=config.verify_ssl,
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

            if ipv4_addr:
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

    def health_check(self, timeout: float = 2.0) -> bool:
        """Check if OPNsense API is accessible."""
        try:
            url = f"{self.base_url}/diagnostics/interface/getInterfaceConfig"
            response = self._client.get(url, timeout=timeout)
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("OPNsense health check failed", error=str(e))
            return False
