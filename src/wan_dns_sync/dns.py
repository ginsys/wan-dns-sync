"""Write A records through octoDNS provider classes.

Only the records this tool owns are ever planned: ``OwnedOnly`` drops every other
record from the provider's existing state, so a zone's other records can never be
deleted or changed, whatever the provider returns.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import structlog
from octodns.processor.base import BaseProcessor
from octodns.provider.base import BaseProvider
from octodns.record import Record
from octodns.zone import Zone

logger = structlog.get_logger()

OwnedKey = tuple[str, str]


class DNSError(Exception):
    """A zone could not be synced safely."""


class WriteUncertainError(DNSError):
    """``provider.apply`` raised, so the write may or may not have reached the zone."""


def load_provider(name: str, args: dict[str, Any]) -> BaseProvider:
    """Instantiate the octoDNS provider named by ``args['class']``."""
    kwargs = dict(args)
    path = str(kwargs.pop("class"))
    module_name, _, class_name = path.rpartition(".")
    if not module_name:
        raise DNSError(f"provider '{name}': '{path}' is not a dotted class path")
    cls = getattr(import_module(module_name), class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, BaseProvider)):
        raise DNSError(f"provider '{name}': '{path}' is not an octoDNS provider class")
    provider: BaseProvider = cls(name, **kwargs)
    return provider


def _forbid_zone_creation(provider: BaseProvider) -> None:
    """Make the provider's own create-zone call fail.

    The listing check above can be stale by the time ``apply`` runs (the zone may be
    deleted in between), and Hetzner's apply then creates it. The providers are made
    fresh for every sync, so replacing the method on this instance is safe.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise DNSError(f"provider {provider.id} tried to create a zone; this tool never does")

    for holder_name, method in (("_client", "zone_create"), ("desec_api", "create_domain")):
        holder = getattr(provider, holder_name, None)
        if holder is not None and hasattr(holder, method):
            setattr(holder, method, refuse)


class OwnedOnly(BaseProcessor):  # type: ignore[misc]
    """Keep only the owned (name, type) pairs in the provider's existing records."""

    def __init__(self, owned: set[OwnedKey]) -> None:
        super().__init__("owned-only")
        self.owned = owned

    def process_target_zone(self, existing: Zone, target: Any, lenient: bool = False) -> Zone:
        for record in list(existing.records):
            if (record.name, record._type) not in self.owned:
                existing.remove_record(record)
        return existing


def sync_zone(
    provider: BaseProvider,
    zone: str,
    owned: dict[str, list[str]],
    ttl: int,
    dry_run: bool,
) -> bool:
    """Make the owned A records of ``zone`` match ``owned`` (name -> addresses).

    Returns True when changes were planned (and applied unless ``dry_run``).
    Raises ``DNSError`` when the zone is missing at the provider: a provider
    would create it on apply, which this tool never wants.
    """
    zone_fqdn = zone.rstrip(".").lower() + "."
    try:
        available = {z.rstrip(".").lower() for z in provider.list_zones()}
    except NotImplementedError as e:
        raise DNSError(f"provider {provider.id} cannot list its zones") from e
    if zone_fqdn.rstrip(".") not in available:
        raise DNSError(f"zone {zone} does not exist at provider {provider.id}")
    _forbid_zone_creation(provider)

    desired = Zone(zone_fqdn, [])
    for name, addresses in owned.items():
        desired.add_record(
            Record.new(
                desired, name, {"type": "A", "ttl": ttl, "values": sorted(addresses)}, lenient=True
            )
        )

    owned_keys = {(name, "A") for name in owned}
    plan = provider.plan(desired, processors=[OwnedOnly(owned_keys)])
    if plan is None:
        logger.debug("No changes", zone=zone, provider=provider.id)
        return False

    stray = [c for c in plan.changes if (c.record.name, c.record._type) not in owned_keys]
    if stray:
        raise DNSError(f"plan for {zone} at {provider.id} touches records this tool does not own")

    logger.info(
        "DNS changes planned",
        zone=zone,
        provider=provider.id,
        changes=[str(c) for c in plan.changes],
        dry_run=dry_run,
    )
    if dry_run:
        return True

    # deSEC derives "exists" from having read a record and would create an
    # existing-but-empty zone; the list_zones check above is the real guard.
    plan.exists = True
    try:
        provider.apply(plan)
    except Exception as e:
        raise WriteUncertainError(f"applying {zone} at {provider.id} failed: {e}") from e
    return True
