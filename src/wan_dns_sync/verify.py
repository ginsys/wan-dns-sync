"""DNS verification by querying the zone's own authoritative nameservers.

Only the IPv4 addresses of the nameservers are queried. The records that are
published are A records, a nameserver serves the same zone on every address, and
an IPv4-only host cannot reach an IPv6 one. Within that, nothing is tolerated: an
address that does not answer correctly, for any reason, fails the verification.
"""

from __future__ import annotations

import dns.flags
import dns.resolver
import structlog

logger = structlog.get_logger()


def _complete(answer: dns.resolver.Answer) -> dns.resolver.Answer:
    """The answer, or ValueError when it is truncated: a partial list omits servers."""
    if answer.response.flags & dns.flags.TC:
        raise ValueError("the answer is truncated")
    return answer


def zone_nameservers(zone: str, timeout: float = 5.0) -> dict[str, list[str] | None]:
    """The zone's nameservers and their IPv4 addresses.

    A nameserver maps to None when its address lookup failed (timeout, SERVFAIL), and
    to an empty list when it has no IPv4 address at all. Both make the caller fail it,
    instead of silently verifying against fewer servers. An empty dict means the NS
    lookup itself failed.
    """
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        names = [str(r.target) for r in _complete(resolver.resolve(zone, "NS"))]
    except Exception as e:  # includes a resolver with no configuration
        logger.warning("Could not look up zone nameservers", zone=zone, error=str(e))
        return {}

    servers: dict[str, list[str] | None] = {}
    for ns in names:
        try:
            servers[ns] = [rdata.address for rdata in _complete(resolver.resolve(ns, "A"))]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            servers[ns] = []
        except Exception as e:
            logger.warning("Could not look up a nameserver address", nameserver=ns, error=str(e))
            servers[ns] = None
    return servers


def _answers(ns_ip: str, fqdn: str, timeout: float) -> set[str]:
    """The A records one nameserver returns for ``fqdn`` (empty on NXDOMAIN or no answer)."""
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [ns_ip]
    resolver.lifetime = timeout
    try:
        answer = resolver.resolve(fqdn, "A")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return set()
    return {rdata.address for rdata in _complete(answer)}


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
        if not ns_ips:  # None: a lookup failed; []: no IPv4 address exists
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
