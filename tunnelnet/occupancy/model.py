"""Swipe events, admission decisions, reservations and occupancy alarms.

Attribution rule (fixed for this system):

* Every event carries the time the card was read at the device (``at``).
  Business truth, the audit trail and the console head count all attribute an
  event to that *event time*, never to the time the server received it.
* ``received_at`` only records when the server first saw the event; it is used
  to tell a late/backfilled event from an online one and to decide whether an
  unmatched swipe can still be paired with a packet still in flight.
* A repeated swipe is never applied as "first subtract, then add". Each
  (person, cabin) pair is a state machine: one IN while outside, one OUT while
  inside; anything else is an anomaly and does not move the head count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

IN = "in"
OUT = "out"
ADJUST = "adjust"

DIRECTIONS = (IN, OUT, ADJUST)

# Ordering rank when two events of the same cabin share one event time.
# OUT sorts before IN so an out/back-in pair frees the slot first and can never
# create a transient head count above capacity; manual adjustments sit between.
_DIRECTION_RANK = {OUT: 0, ADJUST: 1, IN: 2}

# Where an event came from, used by alarms and by the audit projection.
SOURCE_ONLINE = "online"
SOURCE_BACKFILL = "backfill"
SOURCE_MANUAL = "manual"
SOURCES = (SOURCE_ONLINE, SOURCE_BACKFILL, SOURCE_MANUAL)


def direction_rank(direction: str) -> int:
    """Canonical position of a direction inside one event-time tie."""

    return _DIRECTION_RANK.get(direction, len(_DIRECTION_RANK))


def is_direction(value: str) -> bool:
    return value in _DIRECTION_RANK


@dataclass(frozen=True)
class SwipeEvent:
    """One IN/OUT reading at a cabin door, or one signed manual correction."""

    id: str
    cabin_id: str
    person_id: str
    direction: str
    at: str
    seq: int = 0
    device_id: str = ""
    received_at: str = ""
    source: str = SOURCE_ONLINE
    delta: int = 0
    note: str = ""
    server_seq: int = 0

    def order_key(self) -> tuple[str, int, int, str, int, str]:
        """Total ordering inside one cabin ledger.

        event time first (event-time attribution), direction rank second
        (OUT before IN on a tie), then the server-assigned arrival sequence
        (so online reads sharing one device timestamp keep arrival order),
        device sequence, device id and event id for a fully deterministic
        order even with identical clocks.
        """

        return (self.at, direction_rank(self.direction), self.server_seq, self.seq, self.device_id, self.id)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "direction": self.direction,
            "at": self.at,
            "seq": self.seq,
            "device_id": self.device_id,
            "received_at": self.received_at,
            "source": self.source,
            "delta": self.delta,
            "note": self.note,
            "server_seq": self.server_seq,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SwipeEvent":
        return cls(
            id=str(payload.get("id", "")),
            cabin_id=str(payload.get("cabin_id", "")),
            person_id=str(payload.get("person_id", "")),
            direction=str(payload.get("direction", "")),
            at=str(payload.get("at", "")),
            seq=int(payload.get("seq", 0) or 0),
            device_id=str(payload.get("device_id", "")),
            received_at=str(payload.get("received_at", "")),
            source=str(payload.get("source", SOURCE_ONLINE)),
            delta=int(payload.get("delta", 0) or 0),
            note=str(payload.get("note", "")),
            server_seq=int(payload.get("server_seq", 0) or 0),
        )


@dataclass(frozen=True)
class RejectedEvent:
    """An event a batch could not accept, kept out of the immutable ledger."""

    event_id: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"event_id": self.event_id, "reason": self.reason}


@dataclass(frozen=True)
class AdmissionDecision:
    """The gate verdict and the exact head count it was taken against."""

    allowed: bool
    cabin_id: str
    person_id: str
    occupied: int
    capacity: int
    reserved: int
    version: int
    reason: str
    event_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "occupied": self.occupied,
            "capacity": self.capacity,
            "reserved": self.reserved,
            "available": self.capacity - self.occupied - self.reserved,
            "version": self.version,
            "reason": self.reason,
            "event_id": self.event_id,
        }


@dataclass(frozen=True)
class Reservation:
    """A seat promised to an approved entry before the card is swiped."""

    id: str
    cabin_id: str
    person_id: str
    created_at: str
    expires_at: str = ""
    consumed_by: str = ""

    def active(self) -> bool:
        return not self.consumed_by

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "consumed_by": self.consumed_by,
            "active": self.active(),
        }


@dataclass(frozen=True)
class ApprovalRecord:
    """A release/approval verdict frozen against one head count version.

    Approvals never reference a private counter: every row carries the exact
    occupied number, capacity and ledger version the decision was taken
    against, so an approval can always be reconciled against the event log.
    """

    id: str
    cabin_id: str
    person_id: str
    allowed: bool
    occupied: int
    capacity: int
    reserved: int
    version: int
    at: str
    reason: str
    reservation_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "allowed": self.allowed,
            "occupied": self.occupied,
            "capacity": self.capacity,
            "reserved": self.reserved,
            "version": self.version,
            "at": self.at,
            "reason": self.reason,
            "reservation_id": self.reservation_id,
        }


@dataclass(frozen=True)
class Alarm:
    """An occupancy alarm raised against one versioned head count."""

    key: str
    kind: str
    cabin_id: str
    person_id: str
    at: str
    occupied: int
    capacity: int
    version: int
    message: str
    event_id: str = ""
    active: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "kind": self.kind,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "at": self.at,
            "occupied": self.occupied,
            "capacity": self.capacity,
            "version": self.version,
            "message": self.message,
            "event_id": self.event_id,
            "active": self.active,
        }


# Alarm kinds shared by the service, the report and the console.
ALARM_OVER_CAPACITY = "over_capacity"
ALARM_RETROACTIVE_OVER = "retroactive_over_capacity"
ALARM_DUPLICATE_IN = "duplicate_in"
ALARM_DUPLICATE_OUT = "duplicate_out"
ALARM_GHOST_OUT = "ghost_out"
ALARM_OVERSTAY = "overstay"
ALARM_MANUAL = "manual_adjustment"
ALARM_CLOCK = "clock_anomaly"


@dataclass(frozen=True)
class IngestResult:
    """Outcome of ingesting one event or one offline batch."""

    accepted: list[SwipeEvent] = field(default_factory=list)
    duplicates: list[SwipeEvent] = field(default_factory=list)
    rejected: list[RejectedEvent] = field(default_factory=list)
    decisions: list[AdmissionDecision] = field(default_factory=list)
    alarms: list[Alarm] = field(default_factory=list)
    version: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": [event.as_dict() for event in self.accepted],
            "duplicates": [event.as_dict() for event in self.duplicates],
            "rejected": [item.as_dict() for item in self.rejected],
            "decisions": [decision.as_dict() for decision in self.decisions],
            "alarms": [alarm.as_dict() for alarm in self.alarms],
            "version": self.version,
        }
