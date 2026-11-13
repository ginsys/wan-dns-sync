"""DNS verification by querying the zone's own authoritative nameservers."""

from __future__ import annotations

import dns.resolver
import structlog

logger = structlog.get_logger()


def zone_nameservers(zone: str, timeout: float = 5.0) -> dict[str, list[str]]:
    """The zone's nameservers and their addresses (IPv4 and IPv6).

    A nameserver whose address cannot be found maps to an empty list, so the caller
    can fail on it instead of silently verifying against fewer servers. An empty dict
    means the NS lookup itself failed.
    """
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout

    try:
        names = [str(r.target) for r in resolver.resolve(zone, "NS")]
    except Exception as e:
        logger.warning("Could not look up zone nameservers", zone=zone, error=str(e))
        return {}

    servers: dict[str, list[str]] = {}
    for ns in names:
        addresses: list[str] = []
        for rdtype in ("A", "AAAA"):
            try:
                addresses.extend(rdata.address for rdata in resolver.resolve(ns, rdtype))
            except Exception:
                continue
        servers[ns] = addresses
    return servers


def _answers(ns_ip: str, fqdn: str, timeout: float) -> set[str]:
    """The A records one nameserver returns for ``fqdn`` (empty on NXDOMAIN or no answer)."""
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [ns_ip]
    resolver.lifetime = timeout
    try:
        return {rdata.address for rdata in resolver.resolve(fqdn, "A")}
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return set()


def verify_a_records(
    hostname: str,
    zone: str,
    expected_ips: set[str],
    timeout: float = 5.0,
) -> bool:
    """True when every authoritative nameserver of ``zone`` returns ``expected_ips``."""
    fqdn = f"{hostname}.{zone}".rstrip(".")

    servers = zone_nameservers(zone, timeout)
    if not servers:
        logger.warning("No authoritative nameserver to verify against", zone=zone)
        return False

    ok = True
    checked = 0
    for ns, ns_ips in servers.items():
        if not ns_ips:
            logger.warning("Nameserver has no address to verify against", nameserver=ns, zone=zone)
            ok = False
            continue
        for ns_ip in ns_ips:
            checked += 1
            try:
                actual = _answers(ns_ip, fqdn, timeout)
            except Exception as e:
                logger.warning("DNS verification error", nameserver=ns_ip, fqdn=fqdn, error=str(e))
                ok = False
                continue
            if actual != expected_ips:
                logger.warning(
                    "DNS verification mismatch",
                    nameserver=ns_ip,
                    fqdn=fqdn,
                    expected=sorted(expected_ips),
                    actual=sorted(actual),
                )
                ok = False
    if ok:
        logger.debug("DNS verification passed", fqdn=fqdn, addresses=checked)
    return ok
