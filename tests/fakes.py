"""In-memory octoDNS provider for tests."""

from __future__ import annotations

import logging
from typing import Any

from octodns.provider.base import BaseProvider
from octodns.record import Record
from octodns.zone import Zone

RecordData = dict[str, Any]  # {"ttl": int, "values": [...]}


class FakeProvider(BaseProvider):  # type: ignore[misc]
    """Holds zones as {zone: {(name, type): data}}; reads and writes them on demand."""

    SUPPORTS_GEO = False
    SUPPORTS_DYNAMIC = False
    SUPPORTS = {"A", "TXT", "MX"}

    def __init__(self, id: str, zones: dict[str, dict[tuple[str, str], RecordData]] | None = None):
        self.log = logging.getLogger(f"FakeProvider[{id}]")
        super().__init__(id)
        self.zones = zones if zones is not None else {}
        self.applied: list[Any] = []
        self.fail_apply: Exception | None = None

    def _stored(self, zone_name: str) -> dict[tuple[str, str], RecordData] | None:
        """The stored records of a zone, found with or without its trailing dot."""
        for name, records in self.zones.items():
            if name.rstrip(".") == zone_name.rstrip("."):
                return records
        return None

    def list_zones(self) -> list[str]:
        return sorted(self.zones)

    def populate(self, zone: Zone, target: bool = False, lenient: bool = False) -> bool:
        stored = self._stored(zone.name)
        if stored is None:
            return False
        for (name, rtype), data in stored.items():
            zone.add_record(
                Record.new(zone, name, {"type": rtype, **data}, source=self, lenient=True),
                lenient=True,
            )
        return True

    def _apply(self, plan: Any) -> None:
        if self.fail_apply:
            raise self.fail_apply
        self.applied.append(plan)
        stored = self._stored(plan.desired.name)
        if stored is None:
            stored = self.zones.setdefault(plan.desired.name.rstrip("."), {})
        for change in plan.changes:
            key = (change.record.name, change.record._type)
            if change.new is None:
                stored.pop(key, None)
            else:
                stored[key] = {"ttl": change.new.ttl, "values": list(change.new.values)}
