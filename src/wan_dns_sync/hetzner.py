"""Hetzner Cloud DNS client using hcloud library."""

from __future__ import annotations

import structlog
from hcloud import APIException
from hcloud import Client as HCloudClient
from hcloud.zones.client import BoundZone, BoundZoneRRSet
from hcloud.zones.domain import ZoneRecord

from .config import HetznerConfig
from .ratelimit import RateLimiter
from .retry import retry_with_backoff

logger = structlog.get_logger()

# Retryable HTTP status codes
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


class HetznerAPIError(Exception):
    """Custom exception for Hetzner API errors with retry support."""

    def __init__(self, message: str, status_code: int | str | None = None) -> None:
        super().__init__(message)
        # Normalize status_code to int for retryable checks
        if isinstance(status_code, str):
            try:
                self.status_code: int | None = int(status_code)
            except ValueError:
                self.status_code = None
        else:
            self.status_code = status_code


def _is_retryable(exc: Exception) -> bool:
    """Check if exception is retryable."""
    if isinstance(exc, APIException):
        return exc.code in RETRYABLE_STATUS_CODES
    if isinstance(exc, HetznerAPIError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    return False


class HetznerDNSClient:
    """Client for Hetzner Cloud DNS using hcloud library.

    Uses RRSet-based operations for managing A records, with rate limiting
    and retry logic for API resilience.
    """

    def __init__(self, config: HetznerConfig) -> None:
        """Initialize Hetzner DNS client.

        Args:
            config: Hetzner configuration with token, zone, and TTL
        """
        self.config = config
        self._client = HCloudClient(token=config.token)
        self._zones = self._client.zones
        self._zone_cache: dict[str, BoundZone] = {}
        self._rate_limiter = RateLimiter(requests_per_minute=30)

    def close(self) -> None:
        """Close the client (no-op for hcloud, but keeps interface consistent)."""
        pass

    def __enter__(self) -> HetznerDNSClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get_zone(self) -> BoundZone:
        """Get zone by name (cached).

        Returns:
            BoundZone object for the configured zone

        Raises:
            ValueError: If zone not found
        """
        name = self.config.zone
        if name not in self._zone_cache:
            self._rate_limiter.wait()
            try:
                zones = self._zones.get_all(name=name)
            except APIException as e:
                logger.error("Failed to get zone", zone=name, error=str(e))
                raise HetznerAPIError(f"Failed to get zone: {e}", status_code=e.code) from e

            if not zones:
                raise ValueError(f"Zone '{name}' not found in Hetzner DNS")
            self._zone_cache[name] = zones[0]
            logger.debug("Cached zone", zone=name, zone_id=zones[0].id)

        return self._zone_cache[name]

    def _get_a_rrset(self, hostname: str) -> BoundZoneRRSet | None:
        """Get A record RRSet for hostname.

        Args:
            hostname: Record name (without zone suffix)

        Returns:
            BoundZoneRRSet if exists, None otherwise
        """
        zone = self._get_zone()
        self._rate_limiter.wait()

        try:
            rrsets = self._zones.get_rrset_all(zone, name=hostname, type=["A"])
        except APIException as e:
            logger.error("Failed to get RRSet", hostname=hostname, error=str(e))
            raise HetznerAPIError(f"Failed to get RRSet: {e}", status_code=e.code) from e

        return rrsets[0] if rrsets else None

    def get_a_record_ips(self, hostname: str) -> set[str]:
        """Get current A record IPs for hostname.

        Args:
            hostname: Record name (without zone suffix)

        Returns:
            Set of IP addresses for the hostname
        """
        rrset = self._get_a_rrset(hostname)
        if not rrset or not rrset.records:
            return set()
        return {rec.value for rec in rrset.records}

    @retry_with_backoff(
        max_retries=3,
        retryable_exceptions=(HetznerAPIError,),
        should_retry=_is_retryable,
    )
    def sync_a_records(self, hostname: str, desired_ips: list[str], dry_run: bool = False) -> bool:
        """Sync A records for hostname to match desired IPs.

        Uses RRSet operations to efficiently update records:
        - Creates new RRSet if none exists
        - Updates existing RRSet if IPs differ
        - Deletes RRSet if no IPs desired

        Args:
            hostname: Record name (without zone suffix)
            desired_ips: List of desired IP addresses
            dry_run: If True, log changes without applying

        Returns:
            True if changes were made (or would be made in dry_run)
        """
        zone = self._get_zone()
        rrset = self._get_a_rrset(hostname)

        current_ips: set[str] = set()
        if rrset and rrset.records:
            current_ips = {rec.value for rec in rrset.records}

        desired_set = set(desired_ips)

        if current_ips == desired_set:
            logger.debug("No changes needed", hostname=hostname, ips=sorted(current_ips))
            return False

        logger.info(
            "Syncing A records",
            hostname=hostname,
            current=sorted(current_ips),
            desired=sorted(desired_set),
            dry_run=dry_run,
        )

        if dry_run:
            return True

        self._rate_limiter.wait()

        try:
            if not desired_ips:
                # Delete RRSet if no IPs desired
                if rrset:
                    self._zones.delete_rrset(rrset)
                    logger.info("Deleted A record", hostname=hostname)
            elif rrset:
                # Update existing RRSet
                records = [ZoneRecord(value=ip) for ip in sorted(desired_ips)]
                self._zones.set_rrset_records(rrset, records)
                logger.info("Updated A record", hostname=hostname, ips=sorted(desired_ips))
            else:
                # Create new RRSet
                records = [ZoneRecord(value=ip) for ip in sorted(desired_ips)]
                self._zones.create_rrset(
                    zone,
                    name=hostname,
                    type="A",
                    ttl=self.config.ttl,
                    records=records,
                )
                logger.info("Created A record", hostname=hostname, ips=sorted(desired_ips))
        except APIException as e:
            logger.error("Failed to sync A records", hostname=hostname, error=str(e))
            raise HetznerAPIError(f"Failed to sync A records: {e}", status_code=e.code) from e

        return True

    def health_check(self) -> bool:
        """Check if Hetzner API is accessible.

        Returns:
            True if API is accessible and zone exists
        """
        try:
            self._get_zone()
            return True
        except Exception as e:
            logger.error("Hetzner health check failed", error=str(e))
            return False
