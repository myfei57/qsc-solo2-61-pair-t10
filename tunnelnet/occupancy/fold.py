"""Canonical replay of swipe events into one auditable head count.

The fold is the *only* definition of "how many people are in the cabin". The
console, admission, approvals, alarms and the audit export all read numbers
produced here; nobody keeps a private counter. It is a pure function of the
immutable event log, so re-folding after a late event arrives always gives the
same answer a fresh auditor would get by replaying the log.

Rules applied while replaying events in canonical order (event time, then
OUT-before-IN on a tie, then device sequence/id):

* Manual ``adjust`` events always apply their signed delta (they model
  tailgating, escorted visitors and physical corrections). They never change a
  tracked person's IN/OUT state; the caller validates that the result stays
  non-negative.
* ``in`` while the person is already inside is a duplicate IN: the count does
  not move and an anomaly row is produced.
* ``out`` while the person is outside is a ghost OUT: the count does not move.
  It is still produced as an anomaly; whether it is alarm-worthy depends on
  the watermark (an IN paired to it may still be in flight).
* ``in`` while outside always applies, even when it pushes the count past
  capacity. Physical tailgating must stay visible: the count reflects reality
  and the resulting level is marked as an over-capacity violation. Admission
  blocking happens before the door is released, never by hiding an IN that
  physically happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    ADJUST,
    IN,
    OUT,
    Alarm,
    SwipeEvent,
)


@dataclass(frozen=True)
class FoldStep:
    """One event's contribution to the canonical replay."""

    event: SwipeEvent
    applied: bool
    occupied_after: int
    violation: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "event": self.event.as_dict(),
            "applied": self.applied,
            "occupied_after": self.occupied_after,
            "violation": self.violation,
        }


@dataclass(frozen=True)
class FoldResult:
    """Result of replaying one cabin's events up to some point."""

    cabin_id: str
    steps: list[FoldStep] = field(default_factory=list)
    present: frozenset[str] = frozenset()
    occupied: int = 0
    entries: dict[str, SwipeEvent] = field(default_factory=dict)
    anonymous: int = 0

    @property
    def version(self) -> int:
        """Number of steps this fold was computed from."""

        return len(self.steps)

    def step_of(self, event_id: str) -> FoldStep | None:
        for step in self.steps:
            if step.event.id == event_id:
                return step
        return None

    def violations(self) -> list[FoldStep]:
        return [step for step in self.steps if step.violation]


def canonical(events: list[SwipeEvent]) -> list[SwipeEvent]:
    """Order events for replay: event time first, OUT before IN on a tie."""

    return sorted(events, key=lambda event: event.order_key())


def fold(cabin_id: str, events: list[SwipeEvent], capacity: int = 0) -> FoldResult:
    """Replay one cabin's events and return the head count and per-step detail.

    ``capacity`` of zero disables the over-capacity violation markers; the
    running count itself is always computed.
    """

    inside: set[str] = set()
    entries: dict[str, SwipeEvent] = {}
    anonymous = 0
    occupied = 0
    steps: list[FoldStep] = []

    for event in canonical(events):
        violation = ""
        applied = False
        if event.direction == IN:
            if event.person_id in inside:
                violation = "duplicate_in"
            else:
                inside.add(event.person_id)
                entries[event.person_id] = event
                occupied += 1
                applied = True
                if capacity and occupied > capacity:
                    violation = "over_capacity"
        elif event.direction == OUT:
            if event.person_id not in inside:
                violation = "ghost_out"
            else:
                inside.discard(event.person_id)
                entries.pop(event.person_id, None)
                occupied -= 1
                applied = True
        elif event.direction == ADJUST:
            # Manual correction: signed delta for people not tracked by card
            # (tailgaters, escorted visitors). Consumes anonymous occupancy
            # first on the way down so tracked people are not silently removed.
            remaining = event.delta
            while remaining < 0 and anonymous > 0:
                anonymous -= 1
                remaining += 1
            anonymous += max(remaining, 0)
            occupied += event.delta
            applied = True
            if capacity and occupied > capacity:
                violation = "over_capacity"
            if occupied < 0:
                violation = "negative_count"
        steps.append(FoldStep(event=event, applied=applied, occupied_after=occupied, violation=violation))

    return FoldResult(
        cabin_id=cabin_id,
        steps=steps,
        present=frozenset(inside),
        occupied=occupied,
        entries=dict(entries),
        anonymous=anonymous,
    )


def fold_as_of(
    cabin_id: str, events: list[SwipeEvent], as_of: str, capacity: int = 0
) -> FoldResult:
    """Replay only events whose event time is not later than ``as_of``.

    This is the admission view: the gate decides with every event known at the
    server at decision time that claims to have happened no later than the card
    read in front of it. Events with future event times are excluded.
    """

    visible = [event for event in events if event.at <= as_of]
    return fold(cabin_id, visible, capacity)


def present_people(result: FoldResult) -> list[str]:
    """Identifiers of people inside, sorted for stable console output."""

    return sorted(result.present)


def dwell_seconds(result: FoldResult, person_id: str, now: str) -> int:
    """Seconds between a person's last applied IN and ``now``."""

    entered = result.entries.get(person_id)
    if entered is None:
        return 0
    return _stamp_seconds(now) - _stamp_seconds(entered.at)


def _stamp_seconds(stamp: str) -> int:
    import calendar
    import time

    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        digits = "".join(ch for ch in stamp if ch.isdigit())
        return int(digits[:14]) if len(digits) >= 14 else 0


def alarms_from_violations(result: FoldResult, capacity: int) -> list[Alarm]:
    """Turn replay violations into de-duplicable alarm candidates."""

    from .model import (
        ALARM_DUPLICATE_IN,
        ALARM_GHOST_OUT,
        ALARM_OVER_CAPACITY,
    )

    alarms: list[Alarm] = []
    for step in result.steps:
        event = step.event
        if step.violation == "duplicate_in":
            alarms.append(
                Alarm(
                    key=f"{ALARM_DUPLICATE_IN}:{event.id}",
                    kind=ALARM_DUPLICATE_IN,
                    cabin_id=event.cabin_id,
                    person_id=event.person_id,
                    at=event.at,
                    occupied=step.occupied_after,
                    capacity=capacity,
                    version=result.version,
                    message=f"{event.person_id} swiped IN twice without an OUT",
                    event_id=event.id,
                )
            )
        elif step.violation == "ghost_out":
            alarms.append(
                Alarm(
                    key=f"{ALARM_GHOST_OUT}:{event.id}",
                    kind=ALARM_GHOST_OUT,
                    cabin_id=event.cabin_id,
                    person_id=event.person_id,
                    at=event.at,
                    occupied=step.occupied_after,
                    capacity=capacity,
                    version=result.version,
                    message=f"{event.person_id} swiped OUT without a matching IN",
                    event_id=event.id,
                )
            )
        elif step.violation == "over_capacity":
            alarms.append(
                Alarm(
                    key=f"{ALARM_OVER_CAPACITY}:{event.id}",
                    kind=ALARM_OVER_CAPACITY,
                    cabin_id=event.cabin_id,
                    person_id=event.person_id,
                    at=event.at,
                    occupied=step.occupied_after,
                    capacity=capacity,
                    version=result.version,
                    message=(
                        f"head count {step.occupied_after} exceeded capacity {capacity}"
                        f" after {event.direction}"
                    ),
                    event_id=event.id,
                )
            )
    return alarms
