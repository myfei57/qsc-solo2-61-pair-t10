"""Read only occupancy projection shown by the console and exported for audit.

Every number here comes straight from the canonical fold. The projection also
carries the ledger ``version`` (event count) it was computed from, so a page
snapshot and the audit rows produced at the same version can be proven equal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module

# See service.py: the re-exported ``fold`` callable shadows this submodule name.
folding = import_module("tunnelnet.occupancy.fold")

from .service import OccupancyService  # noqa: E402


@dataclass(frozen=True)
class CabinOccupancy:
    """Head count row for one cabin."""

    cabin_id: str
    occupied: int
    capacity: int
    available: int
    reserved: int
    present: list[str]
    anonymous: int
    version: int
    at_capacity: bool
    over: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "cabin_id": self.cabin_id,
            "occupied": self.occupied,
            "capacity": self.capacity,
            "available": self.available,
            "reserved": self.reserved,
            "present": self.present,
            "anonymous": self.anonymous,
            "version": self.version,
            "at_capacity": self.at_capacity,
            "over": self.over,
        }


@dataclass(frozen=True)
class OccupancyState:
    """Whole service projection: cabin rows, records and alarms."""

    cabins: list[CabinOccupancy] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    approvals: list[dict[str, object]] = field(default_factory=list)
    reservations: list[dict[str, object]] = field(default_factory=list)
    alarms: list[dict[str, object]] = field(default_factory=list)
    total_occupied: int = 0
    active_alarms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "cabins": [row.as_dict() for row in self.cabins],
            "events": self.events,
            "approvals": self.approvals,
            "reservations": self.reservations,
            "alarms": self.alarms,
            "total_occupied": self.total_occupied,
            "active_alarms": self.active_alarms,
        }

    def cabin(self, cabin_id: str) -> CabinOccupancy | None:
        for row in self.cabins:
            if row.cabin_id == cabin_id:
                return row
        return None


def cabin_row(service: OccupancyService, cabin_id: str, now: str = "") -> CabinOccupancy:
    result = service.fold(cabin_id)
    capacity = service.capacity_of(cabin_id)
    reserved = service._reservations.active_count(cabin_id, now or service._clock())
    occupied = result.occupied
    available = max(capacity - occupied - reserved, 0)
    return CabinOccupancy(
        cabin_id=cabin_id,
        occupied=occupied,
        capacity=capacity,
        available=available,
        reserved=reserved,
        present=folding.present_people(result),
        anonymous=result.anonymous,
        version=result.version,
        at_capacity=occupied + reserved >= capacity > 0,
        over=occupied > capacity > 0,
    )


def collect(service: OccupancyService, now: str = "") -> OccupancyState:
    """Build the full projection from one canonical fold per cabin."""

    now = now or service._clock()
    rows = [cabin_row(service, cabin_id, now) for cabin_id in service.cabins()]
    events = [event.as_dict() for event in folding.canonical(service.events())]
    approvals = [record.as_dict() for record in service.approvals()]
    reservations = [item.as_dict() for item in service.reservations()]
    alarms = [alarm.as_dict() for alarm in service.all_alarms()]
    return OccupancyState(
        cabins=rows,
        events=events,
        approvals=approvals,
        reservations=reservations,
        alarms=alarms,
        total_occupied=sum(row.occupied for row in rows),
        active_alarms=len([alarm for alarm in alarms if alarm["active"]]),
    )
