"""DNS verification by querying the zone's own authoritative nameservers."""

from __future__ import annotations

import dns.resolver
import structlog

logger = structlog.get_logger()


def zone_nameserver_ips(zone: str, timeout: float = 5.0) -> list[str]:
    """Addresses of the zone's authoritative nameservers (empty when none resolve)."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout

    ips: list[str] = []
    try:
        names = [str(r.target) for r in resolver.resolve(zone, "NS")]
    except Exception as e:
        logger.warning("Could not look up zone nameservers", zone=zone, error=str(e))
        return ips
    for ns in names:
        try:
            ips.extend(rdata.address for rdata in resolver.resolve(ns, "A"))
        except Exception:
            continue
    return ips


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

    ns_ips = zone_nameserver_ips(zone, timeout)
    if not ns_ips:
        logger.warning("No authoritative nameserver to verify against", zone=zone)
        return False

    ok = True
    for ns_ip in ns_ips:
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
        logger.debug("DNS verification passed", fqdn=fqdn, nameservers=len(ns_ips))
    return ok
