"""DNS verification by querying the zone's own authoritative nameservers."""

from __future__ import annotations

import dns.exception
import dns.resolver
import structlog

logger = structlog.get_logger()


def zone_nameservers(zone: str, timeout: float = 5.0) -> dict[str, list[str] | None]:
    """The zone's nameservers and their addresses (IPv4 and IPv6).

    A nameserver maps to None when an address lookup failed (timeout, SERVFAIL), and
    to an empty list when it has no address at all. Both make the caller fail it,
    instead of silently verifying against fewer servers. Only a confirmed "no such
    record" (NoAnswer, NXDOMAIN) counts as the absence of an address family. An empty
    dict means the NS lookup itself failed.
    """
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        names = [str(r.target) for r in resolver.resolve(zone, "NS")]
    except Exception as e:  # includes a resolver with no configuration
        logger.warning("Could not look up zone nameservers", zone=zone, error=str(e))
        return {}

    servers: dict[str, list[str] | None] = {}
    for ns in names:
        addresses: list[str] = []
        failed = False
        for rdtype in ("A", "AAAA"):
            try:
                found = [rdata.address for rdata in resolver.resolve(ns, rdtype)]
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except Exception as e:
                logger.warning(
                    "Could not look up a nameserver address",
                    nameserver=ns,
                    rdtype=rdtype,
                    error=str(e),
                )
                failed = True
                break
            addresses.extend(found)
        servers[ns] = None if failed else addresses
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
    except dns.resolver.NoNameservers as e:
        # dnspython raises this both for a server it could not talk to and for one that
        # answered SERVFAIL or REFUSED. Only the first is "unreachable".
        if any(error[4] is not None for error in e.kwargs["errors"]):
            raise
        raise UnreachableError(str(e)) from e


class UnreachableError(Exception):
    """No answer could be obtained from an address: no route, refused connection, timeout."""


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
        if not ns_ips:  # None: a lookup failed; []: no address exists
            logger.warning("Nameserver has no address to verify against", nameserver=ns, zone=zone)
            ok = False
            continue
        answered = False
        for ns_ip in ns_ips:
            try:
                actual = _answers(ns_ip, fqdn, timeout)
            except (UnreachableError, OSError, dns.exception.Timeout) as e:
                # An address this host cannot reach (an IPv6 one without a route) is no
                # verdict on the nameserver; it fails only when none of its addresses answers.
                logger.warning("DNS verification error", nameserver=ns_ip, fqdn=fqdn, error=str(e))
                continue
            except Exception as e:  # the server answered, with a failure
                logger.warning("DNS verification failed", nameserver=ns_ip, fqdn=fqdn, error=str(e))
                ok = False
                answered = True  # it did answer: no second "no address answered" report
                continue
            answered = True
            checked += 1
            if actual != expected_ips:
                logger.warning(
                    "DNS verification mismatch",
                    nameserver=ns_ip,
                    fqdn=fqdn,
                    expected=sorted(expected_ips),
                    actual=sorted(actual),
                )
                ok = False
        if not answered:
            logger.warning("No address of the nameserver answered", nameserver=ns, zone=zone)
            ok = False
    if ok:
        logger.debug("DNS verification passed", fqdn=fqdn, addresses=checked)
    return ok
