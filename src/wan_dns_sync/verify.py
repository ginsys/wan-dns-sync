"""DNS verification by querying Hetzner authoritative nameservers."""

from __future__ import annotations

import dns.resolver
import structlog

logger = structlog.get_logger()

# Hetzner's authoritative nameservers
HETZNER_NAMESERVERS = [
    "helium.ns.hetzner.de",
    "hydrogen.ns.hetzner.com",
    "oxygen.ns.hetzner.com",
]


def resolve_nameserver_ips(timeout: float = 5.0) -> list[str]:
    """Resolve Hetzner NS hostnames to IP addresses.

    Args:
        timeout: DNS query timeout in seconds

    Returns:
        List of nameserver IP addresses
    """
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout

    ns_ips: list[str] = []
    for ns in HETZNER_NAMESERVERS:
        try:
            answers = resolver.resolve(ns, "A")
            ns_ips.extend([rdata.address for rdata in answers])
        except Exception:
            continue

    return ns_ips


def verify_a_records(
    hostname: str,
    zone: str,
    expected_ips: set[str],
    timeout: float = 5.0,
) -> bool:
    """Verify A records match expected IPs by querying Hetzner NS directly.

    Queries the authoritative Hetzner nameservers to verify DNS changes
    have propagated correctly.

    Args:
        hostname: Record name (without zone suffix)
        zone: DNS zone name
        expected_ips: Set of expected IP addresses
        timeout: DNS query timeout in seconds

    Returns:
        True if DNS matches expected, False otherwise
    """
    fqdn = f"{hostname}.{zone}".rstrip(".")

    ns_ips = resolve_nameserver_ips(timeout)
    if not ns_ips:
        logger.warning("Could not resolve any Hetzner nameservers")
        return False

    resolver = dns.resolver.Resolver()
    resolver.nameservers = ns_ips
    resolver.lifetime = timeout

    try:
        answers = resolver.resolve(fqdn, "A")
        resolved_ips = {rdata.address for rdata in answers}

        if resolved_ips == expected_ips:
            logger.debug(
                "DNS verification passed",
                hostname=hostname,
                zone=zone,
                ips=sorted(resolved_ips),
            )
            return True

        logger.warning(
            "DNS verification mismatch",
            hostname=hostname,
            zone=zone,
            expected=sorted(expected_ips),
            actual=sorted(resolved_ips),
        )
        return False

    except dns.resolver.NXDOMAIN:
        # Record doesn't exist - correct if we expect no IPs
        if not expected_ips:
            logger.debug(
                "DNS verification passed (NXDOMAIN expected)",
                hostname=hostname,
                zone=zone,
            )
            return True
        logger.warning(
            "DNS verification failed: NXDOMAIN",
            hostname=hostname,
            zone=zone,
            expected=sorted(expected_ips),
        )
        return False

    except dns.resolver.NoAnswer:
        # No A records for hostname
        if not expected_ips:
            logger.debug(
                "DNS verification passed (no A records expected)",
                hostname=hostname,
                zone=zone,
            )
            return True
        logger.warning(
            "DNS verification failed: no A records",
            hostname=hostname,
            zone=zone,
            expected=sorted(expected_ips),
        )
        return False

    except Exception as e:
        logger.error(
            "DNS verification error",
            hostname=hostname,
            zone=zone,
            error=str(e),
        )
        return False
