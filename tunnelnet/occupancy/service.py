"""Cabin occupancy service: head counting, capacity admission and alarms.

One immutable swipe ledger is the system of record. Every number the console
shows, every approval and every alarm is produced by folding that ledger, so
they cannot disagree with the audit trail.

The two semantic choices this module implements are:

1. Event-time attribution. Events are ordered by the device event time (OUT
   before IN on an exact tie), not by server arrival time. A late/backfilled
   event is appended and the cabin is re-folded; it can correct the count and
   raise a retroactive alarm but it never rewrites a release already decided.
2. State-machine swiping, never "first subtract then add". A person IN swiping
   IN again is a duplicate (no count change, alarm); a person OUT swiping OUT
   again or OUT with no IN is a ghost OUT (no count change, alarm once the
   watermark passes); the same person's OUT/IN on one timestamp applies OUT
   first so capacity is freed before the re-entry.

All operations for one cabin take one per-cabin lock, giving serialisable
admission under concurrent card reads.
"""

from __future__ import annotations

import threading
import uuid
from importlib import import_module
from typing import Protocol

from tunnelnet.store import Store

# Imported as a module explicitly: the package __init__ re-exports a ``fold``
# function with the same name, which would shadow this submodule attribute.
folding = import_module("tunnelnet.occupancy.fold")

from .alarms import AlarmRegistry
from .ledger import EventLedger, validate_event
from .model import (
    ADJUST,
    IN,
    ALARM_MANUAL,
    ALARM_RETROACTIVE_OVER,
    SOURCE_BACKFILL,
    SOURCE_MANUAL,
    SOURCE_ONLINE,
    AdmissionDecision,
    Alarm,
    ApprovalRecord,
    IngestResult,
    RejectedEvent,
    Reservation,
    SwipeEvent,
)
from .reservations import ReservationBook

DEFAULT_LATE_WINDOW_SECONDS = 300
DEFAULT_APPROVAL_TTL_SECONDS = 600
DEFAULT_DWELL_LIMIT_SECONDS = 8 * 60 * 60


class Clock(Protocol):
    """Returns the current server time; injectable for deterministic tests."""

    def __call__(self) -> str:  # pragma: no cover - structural
        ...


def _clock() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class OccupancyService:
    """Owns capacities, the swipe ledger, reservations, approvals and alarms."""

    def __init__(
        self,
        store: Store | None = None,
        clock: Clock = _clock,
        late_window_seconds: int = DEFAULT_LATE_WINDOW_SECONDS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._late_window_seconds = late_window_seconds
        self._ledger = EventLedger()
        self._reservations = ReservationBook()
        self._alarms = AlarmRegistry()
        self._capacities: dict[str, int] = {}
        self._dwell_limits: dict[str, int] = {}
        self._approvals: list[ApprovalRecord] = []
        self._lock = threading.RLock()
        if store is not None:
            self._recover()

    # ------------------------------------------------------------------ setup

    def configure_cabin(self, cabin_id: str, capacity: int, dwell_limit_seconds: int = 0) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        with self._lock:
            self._capacities[cabin_id] = capacity
            if dwell_limit_seconds > 0:
                self._dwell_limits[cabin_id] = dwell_limit_seconds
            self._ledger.lock_for(cabin_id)

    def capacity_of(self, cabin_id: str) -> int:
        return self._capacities.get(cabin_id, 0)

    def dwell_limit(self, cabin_id: str) -> int:
        return self._dwell_limits.get(cabin_id, DEFAULT_DWELL_LIMIT_SECONDS)

    def cabins(self) -> list[str]:
        with self._lock:
            return sorted(set(self._capacities) | set(self._ledger.cabins()))

    def _require_capacity(self, cabin_id: str) -> int:
        capacity = self.capacity_of(cabin_id)
        if capacity <= 0:
            raise ValueError(f"cabin {cabin_id} has no occupancy capacity configured")
        return capacity

    # ------------------------------------------------------------- projection

    def fold(self, cabin_id: str) -> folding.FoldResult:
        """Canonical head count of one cabin, straight off the event log."""

        return folding.fold(cabin_id, self._ledger.events(cabin_id), self.capacity_of(cabin_id))

    def occupied(self, cabin_id: str) -> int:
        return self.fold(cabin_id).occupied

    def present(self, cabin_id: str) -> list[str]:
        return folding.present_people(self.fold(cabin_id))

    def version(self, cabin_id: str) -> int:
        return self._ledger.count(cabin_id)

    def available(self, cabin_id: str, now: str = "") -> int:
        capacity = self._require_capacity(cabin_id)
        now = now or self._clock()
        with self._ledger.lock_for(cabin_id):
            occupied = self.occupied(cabin_id)
            reserved = self._reservations.active_count(cabin_id, now)
        return capacity - occupied - reserved

    # ------------------------------------------------------------ online gate

    def seed_event(self, event: SwipeEvent) -> bool:
        """Place a boot-time demo event into memory without persisting it.

        Production events always go through :meth:`swipe`/``backfill`` which
        persist; this only seeds the in-memory ledger for the console demo so a
        fresh data directory stays empty.
        """

        validate_event(event)
        return self._ledger.append(event)

    def reset_records(self) -> None:
        """Drop every in-memory ledger record, approval, reservation and alarm.

        Capacity configuration is retained. Used by the console factory reset,
        which has already deleted the matching documents in the file store.
        """

        with self._lock:
            self._ledger = EventLedger()
            self._reservations = ReservationBook()
            self._alarms = AlarmRegistry()
            self._approvals = []
            for cabin_id in self._capacities:
                self._ledger.lock_for(cabin_id)

    def swipe(
        self,
        cabin_id: str,
        person_id: str,
        direction: str,
        event_id: str = "",
        at: str = "",
        seq: int = 0,
        device_id: str = "",
    ) -> IngestResult:
        """Handle one online card read: decide admission, then append if real.

        IN beyond capacity is refused *before* it is accepted as an event: the
        door was not released, so no physical IN happened. OUT is always
        accepted (egress must never be blocked).
        """

        event_id = event_id or uuid.uuid4().hex
        at = at or self._clock()
        event = SwipeEvent(
            id=event_id,
            cabin_id=cabin_id,
            person_id=person_id,
            direction=direction,
            at=at,
            seq=seq,
            device_id=device_id,
            received_at=self._clock(),
            source=SOURCE_ONLINE,
        )
        validate_event(event)

        def action() -> IngestResult:
            if self._ledger.has(event_id):
                duplicate = self._ledger.get(event_id)
                return IngestResult(duplicates=[duplicate] if duplicate else [], version=self._ledger.count(cabin_id))
            capacity = self._require_capacity(cabin_id)
            current_event = SwipeEvent(
                **{**event.__dict__, "server_seq": self._ledger.count(cabin_id) + 1}
            )

            # An online gate decides against the confirmed state in front of it
            # (the cabin lock serialises reads): that is the head count at the
            # arrival instant. Event-time attribution governs the durable log
            # and late events, not what the door could have known live.
            result = self.fold(cabin_id)

            # OUT (egress) is always accepted and never blocked; a ghost OUT is
            # recorded unapplied and its alarm is gated by the late window.
            if direction != IN:
                self._ledger.append(current_event)
                self._persist_event(current_event)
                refold = self._refold_cabin(cabin_id, capacity)
                decision = AdmissionDecision(
                    allowed=True,
                    cabin_id=cabin_id,
                    person_id=person_id,
                    occupied=refold.occupied,
                    capacity=capacity,
                    reserved=self._reservations.active_count(cabin_id, current_event.received_at),
                    version=refold.version,
                    reason="egress",
                    event_id=current_event.id,
                )
                return IngestResult(
                    accepted=[current_event], decisions=[decision], alarms=refold.alarms, version=refold.version
                )

            # A person already inside swiping IN again: no second seat, no
            # release. The read is kept as an audit anomaly.
            if person_id in result.present:
                self._ledger.append(current_event)
                self._persist_event(current_event)
                refold = self._refold_cabin(cabin_id, capacity)
                decision = AdmissionDecision(
                    allowed=False,
                    cabin_id=cabin_id,
                    person_id=person_id,
                    occupied=refold.occupied,
                    capacity=capacity,
                    reserved=self._reservations.active_count(cabin_id, current_event.received_at),
                    version=refold.version,
                    reason="already inside",
                    event_id=current_event.id,
                )
                self._record_approval(decision)
                return IngestResult(
                    accepted=[current_event], decisions=[decision], alarms=refold.alarms, version=refold.version
                )

            # Capacity admission: refusal happens before any physical IN, so the
            # event is not appended; only the durable denial remains.
            decision = self._admission(cabin_id, person_id, capacity, current_event, result)
            if not decision.allowed:
                self._record_approval(decision)
                return IngestResult(decisions=[decision], version=self._ledger.count(cabin_id))

            self._ledger.append(current_event)
            self._persist_event(current_event)
            reservation = self._reservations.find_for_person(cabin_id, person_id, current_event.received_at)
            if reservation is not None:
                self._reservations.consume(reservation.id, current_event.id)
            refold = self._refold_cabin(cabin_id, capacity)
            decision = AdmissionDecision(
                allowed=True,
                cabin_id=cabin_id,
                person_id=person_id,
                occupied=refold.occupied,
                capacity=capacity,
                reserved=self._reservations.active_count(cabin_id, current_event.received_at),
                version=refold.version,
                reason="released",
                event_id=current_event.id,
            )
            self._record_approval(decision)
            return IngestResult(
                accepted=[current_event], decisions=[decision], alarms=refold.alarms, version=refold.version
            )

        return self._ledger.synchronized(cabin_id, action)  # type: ignore[return-value]

    def _admission(
        self,
        cabin_id: str,
        person_id: str,
        capacity: int,
        event: SwipeEvent,
        current: folding.FoldResult | None = None,
    ) -> AdmissionDecision:
        """The capacity verdict for an IN against the confirmed head count.

        Reservations hold seats by being counted as future occupancy; the
        reservation belonging to this person is redeemed by this very swipe and
        therefore is not counted against them.
        """

        now = event.received_at or self._clock()
        result = current if current is not None else self.fold(cabin_id)
        reserved = self._reservations.active_count(cabin_id, now)
        if self._reservations.find_for_person(cabin_id, person_id, now):
            reserved -= 1
        reserved = max(reserved, 0)
        occupied = result.occupied
        allowed = occupied + reserved + 1 <= capacity
        reason = "seat available" if allowed else "cabin at capacity"
        return AdmissionDecision(
            allowed=allowed,
            cabin_id=cabin_id,
            person_id=person_id,
            occupied=occupied,
            capacity=capacity,
            reserved=reserved,
            version=result.version,
            reason=reason,
            event_id=event.id,
        )

    # -------------------------------------------------------------- approvals

    def approve(
        self,
        cabin_id: str,
        person_id: str,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        approval_id: str = "",
        at: str = "",
    ) -> ApprovalRecord:
        """Grant an entry only while a seat is free, reserving that seat.

        The approval row is persisted before any door release and carries the
        exact fold version/occupied value, so releases and the audit log quote
        the same number. At capacity the approval is denied and no seat is held.
        """

        now = at or self._clock()

        def action() -> ApprovalRecord:
            capacity = self._require_capacity(cabin_id)
            result = self.fold(cabin_id)
            reserved = self._reservations.active_count(cabin_id, now)
            allowed = result.occupied + reserved + 1 <= capacity
            record = ApprovalRecord(
                id=approval_id or uuid.uuid4().hex,
                cabin_id=cabin_id,
                person_id=person_id,
                allowed=allowed,
                occupied=result.occupied,
                capacity=capacity,
                reserved=reserved,
                version=result.version,
                at=now,
                reason="seat reserved" if allowed else "cabin at capacity",
            )
            if allowed:
                expires_at = self._add_seconds(now, ttl_seconds)
                reservation = Reservation(
                    id=uuid.uuid4().hex,
                    cabin_id=cabin_id,
                    person_id=person_id,
                    created_at=now,
                    expires_at=expires_at,
                )
                self._reservations.add(reservation)
                record = ApprovalRecord(
                    id=record.id,
                    cabin_id=cabin_id,
                    person_id=person_id,
                    allowed=True,
                    occupied=result.occupied,
                    capacity=capacity,
                    reserved=reserved + 1,
                    version=result.version,
                    at=now,
                    reason=record.reason,
                    reservation_id=reservation.id,
                )
                if self._store is not None:
                    from .persistence import save_reservation

                    save_reservation(self._store, reservation)
            self._record_approval(record)
            return record

        return self._ledger.synchronized(cabin_id, action)  # type: ignore[return-value]

    def approvals(self, cabin_id: str = "") -> list[ApprovalRecord]:
        with self._lock:
            return [item for item in self._approvals if not cabin_id or item.cabin_id == cabin_id]

    def _record_approval(self, decision: AdmissionDecision | ApprovalRecord) -> ApprovalRecord:
        if isinstance(decision, ApprovalRecord):
            record = decision
        else:
            record = ApprovalRecord(
                id=uuid.uuid4().hex,
                cabin_id=decision.cabin_id,
                person_id=decision.person_id,
                allowed=decision.allowed,
                occupied=decision.occupied,
                capacity=decision.capacity,
                reserved=decision.reserved,
                version=decision.version,
                at=self._clock(),
                reason=decision.reason,
            )
        with self._lock:
            self._approvals.append(record)
        if self._store is not None:
            from .persistence import save_approval

            save_approval(self._store, record)
        return record

    # ------------------------------------------------------ offline backfill

    def ingest_batch(self, events: list[SwipeEvent]) -> IngestResult:
        """Apply an offline device batch with per-event partial failure.

        Each event is validated and ingested independently: one bad event is
        reported as rejected and never stops the rest. Replaying the same
        batch (at-least-once delivery) returns the stored events as duplicates
        and changes nothing, so retries and device backfills are idempotent.
        Events are processed under their cabin locks in canonical event order.
        """

        accepted: list[SwipeEvent] = []
        duplicates: list[SwipeEvent] = []
        rejected: list[RejectedEvent] = []
        decisions: list[AdmissionDecision] = []
        alarms: list[Alarm] = []
        version = 0

        valid: list[SwipeEvent] = []
        for event in events:
            try:
                validate_event(event)
            except ValueError as exc:
                rejected.append(RejectedEvent(event_id=getattr(event, "id", ""), reason=str(exc)))
                continue
            valid.append(event)

        grouped = self._group_by_cabin(valid)
        for cabin_id in sorted(grouped):
            result = self._ingest_cabin_batch(cabin_id, grouped[cabin_id])
            accepted.extend(result.accepted)
            duplicates.extend(result.duplicates)
            rejected.extend(result.rejected)
            decisions.extend(result.decisions)
            alarms.extend(result.alarms)
            version = max(version, result.version)
        return IngestResult(
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            decisions=decisions,
            alarms=alarms,
            version=version,
        )

    def _ingest_cabin_batch(self, cabin_id: str, events: list[SwipeEvent]) -> IngestResult:
        def action() -> IngestResult:
            capacity = self.capacity_of(cabin_id)
            accepted: list[SwipeEvent] = []
            duplicates: list[SwipeEvent] = []
            rejected: list[RejectedEvent] = []
            decisions: list[AdmissionDecision] = []
            batch_alarms: list[Alarm] = []

            # Canonical event order per cabin so a batch's internal out/in
            # pairing is applied deterministically regardless of send order.
            ordered = folding.canonical(events)
            for event in ordered:
                if not event.received_at:
                    event = SwipeEvent(**{**event.__dict__, "received_at": self._clock()})
                if self._ledger.has(event.id):
                    stored = self._ledger.get(event.id)
                    if stored is not None:
                        duplicates.append(stored)
                    continue
                if not capacity:
                    rejected.append(
                        RejectedEvent(event_id=event.id, reason=f"cabin {cabin_id} has no capacity configured")
                    )
                    continue
                self._ledger.append(event)
                accepted.append(event)
                self._persist_event(event)
                decision = AdmissionDecision(
                    allowed=True,
                    cabin_id=cabin_id,
                    person_id=event.person_id,
                    occupied=0,
                    capacity=capacity,
                    reserved=0,
                    version=self._ledger.count(cabin_id),
                    reason="backfilled",
                    event_id=event.id,
                )
                decisions.append(decision)
                self._check_clock(event, batch_alarms)

            if accepted:
                folded = self._refold_cabin(cabin_id, capacity)
                updated: list[AdmissionDecision] = []
                for decision in decisions:
                    step = folded.result.step_of(decision.event_id)
                    updated.append(
                        AdmissionDecision(
                            allowed=decision.allowed,
                            cabin_id=decision.cabin_id,
                            person_id=decision.person_id,
                            occupied=step.occupied_after if step is not None else decision.occupied,
                            capacity=decision.capacity,
                            reserved=decision.reserved,
                            version=folded.version,
                            reason=decision.reason,
                            event_id=decision.event_id,
                        )
                    )
                decisions = updated
                batch_alarms.extend(folded.alarms)
            return IngestResult(
                accepted=accepted,
                duplicates=duplicates,
                rejected=rejected,
                decisions=decisions,
                alarms=self._dedupe_alarms(batch_alarms),
                version=self._ledger.count(cabin_id),
            )

        return self._ledger.synchronized(cabin_id, action)  # type: ignore[return-value]

    def backfill(
        self,
        cabin_id: str,
        person_id: str,
        direction: str,
        at: str,
        event_id: str = "",
        seq: int = 0,
        device_id: str = "",
    ) -> IngestResult:
        """Convenience wrapper for one offline/backfilled swipe event."""

        event = SwipeEvent(
            id=event_id or uuid.uuid4().hex,
            cabin_id=cabin_id,
            person_id=person_id,
            direction=direction,
            at=at,
            seq=seq,
            device_id=device_id,
            received_at=self._clock(),
            source=SOURCE_BACKFILL,
        )
        return self.ingest_batch([event])

    # --------------------------------------------------------- manual fixing

    def adjust(
        self,
        cabin_id: str,
        delta: int,
        note: str,
        person_id: str = "operator",
        at: str = "",
        event_id: str = "",
    ) -> IngestResult:
        """Apply a signed manual correction (tailgate/escort reconciliation).

        A correction that would drive the count below zero is rejected rather
        than silently recorded; every accepted adjustment is itself an event
        in the log and raises an alarm, so manual fixes stay auditable.
        """

        if delta == 0:
            raise ValueError("adjustment delta must be non-zero")
        now = at or self._clock()

        def action() -> IngestResult:
            capacity = self._require_capacity(cabin_id)
            current = self.occupied(cabin_id)
            if current + delta < 0:
                raise ValueError(
                    f"adjustment {delta} would take cabin below zero (occupied={current})"
                )
            event = SwipeEvent(
                id=event_id or uuid.uuid4().hex,
                cabin_id=cabin_id,
                person_id=person_id,
                direction=ADJUST,
                at=now,
                received_at=self._clock(),
                source=SOURCE_MANUAL,
                delta=delta,
                note=note,
            )
            self._ledger.append(event)
            self._persist_event(event)
            result = self._refold_cabin(cabin_id, capacity)
            return IngestResult(accepted=[event], alarms=result.alarms, version=result.version)

        return self._ledger.synchronized(cabin_id, action)  # type: ignore[return-value]

    # ------------------------------------------------------------- sweeps etc

    def sweep(self, now: str = "") -> dict[str, object]:
        """Release expired reservations and raise overstay alarms.

        Idempotent: run as often as desired; alarm keys make repeated sweeps
        raise nothing new.
        """

        now = now or self._clock()
        expired = self._reservations.sweep(now)
        overstays: list[Alarm] = []
        for cabin_id in self.cabins():
            limit = self.dwell_limit(cabin_id)
            result = self.fold(cabin_id)
            for person_id in result.entries:
                seconds = folding.dwell_seconds(result, person_id, now)
                if seconds >= limit:
                    entered = result.entries[person_id]
                    alarm = Alarm(
                        key=f"overstay:{cabin_id}:{person_id}:{entered.id}",
                        kind="overstay",
                        cabin_id=cabin_id,
                        person_id=person_id,
                        at=now,
                        occupied=result.occupied,
                        capacity=self.capacity_of(cabin_id),
                        version=result.version,
                        message=(
                            f"{person_id} has been in {cabin_id} for {seconds}s"
                            f" (limit {limit}s)"
                        ),
                        event_id=entered.id,
                    )
                    if self._alarms.raise_alarm(alarm):
                        self._persist_alarm(alarm)
                        overstays.append(alarm)
        return {
            "at": now,
            "expired": [item.as_dict() for item in expired],
            "overstay_alarms": [item.as_dict() for item in overstays],
        }

    def alarms(self, cabin_id: str = "") -> list[Alarm]:
        return self._alarms.active(cabin_id)

    def all_alarms(self) -> list[Alarm]:
        return self._alarms.all()

    def acknowledge_alarm(self, key: str) -> bool:
        if not self._alarms.clear(key):
            return False
        alarm = self._alarms.get(key)
        if alarm is not None and self._store is not None:
            from .persistence import save_alarm

            save_alarm(self._store, alarm)
        return True

    def events(self, cabin_id: str = "") -> list[SwipeEvent]:
        if cabin_id:
            return folding.canonical(self._ledger.events(cabin_id))
        return self._ledger.all_events()

    def reservations(self, cabin_id: str = "") -> list[Reservation]:
        now = self._clock()
        items = [
            item for item in self._reservations.all() if not cabin_id or item.cabin_id == cabin_id
        ]
        return [item for item in items if item.active() and not ReservationBook._expired(item, now)]

    def ledger_count(self) -> int:
        return self._ledger.count()

    # ---------------------------------------------------------------- helpers

    def _persist_event(self, event: SwipeEvent) -> None:
        if self._store is not None:
            from .persistence import save_event

            save_event(self._store, event)

    def _refold_cabin(self, cabin_id: str, capacity: int) -> "_Refold":
        """Refold one cabin and persist the event + raise canonical alarms."""

        result = self.fold(cabin_id)
        candidates = folding.alarms_from_violations(result, capacity)
        raised = self._raise_late_aware_alarms(cabin_id, result, candidates)
        return _Refold(occupied=result.occupied, version=result.version, alarms=raised, result=result)

    def _raise_late_aware_alarms(
        self, cabin_id: str, result: folding.FoldResult, candidates: list[Alarm]
    ) -> list[Alarm]:
        """Suppress anomalies a still-in-flight paired event could fix and
        classify every over-capacity level as live or retroactive.

        * A ghost OUT is only alarming once the late window for its matching IN
          has passed.
        * A duplicate IN is always anomalous (the person is already inside).
        * Over-capacity is impossible on the online path (the gate refuses), so
          an over-capacity level in the fold necessarily contains a late or
          manual event that the gate never blocked: it is raised as a
          retroactive over-capacity alarm against the late event.
        """

        now = self._clock()
        raised: list[Alarm] = []
        for alarm in candidates:
            event = self._ledger.get(alarm.event_id)
            if event is None:
                continue
            if alarm.kind == "ghost_out":
                age = self._seconds_between(event.at, now)
                if age < self._late_window_seconds:
                    continue
            if alarm.kind == "over_capacity":
                retroactive = self._retroactive_over(alarm, result)
                if retroactive is None:
                    # A genuinely live over-capacity (should not happen with the
                    # gate closed): keep the original alarm.
                    pass
                else:
                    event = retroactive
                    alarm = Alarm(
                        key=f"retroactive_over_capacity:{event.id}",
                        kind=ALARM_RETROACTIVE_OVER,
                        cabin_id=alarm.cabin_id,
                        person_id=alarm.person_id,
                        at=alarm.at,
                        occupied=alarm.occupied,
                        capacity=alarm.capacity,
                        version=alarm.version,
                        message=(
                            f"late event {event.id} ({event.source} at {event.at}) shows the cabin"
                            f" reached {alarm.occupied} > {alarm.capacity} after the fact"
                        ),
                        event_id=event.id,
                    )
            if self._alarms.raise_alarm(alarm):
                self._persist_alarm(alarm)
                raised.append(alarm)
        # Manual adjustments always raise their own audit alarm.
        for step in result.steps:
            if step.event.direction == ADJUST and step.event.source == SOURCE_MANUAL:
                alarm = Alarm(
                    key=f"manual_adjustment:{step.event.id}",
                    kind=ALARM_MANUAL,
                    cabin_id=cabin_id,
                    person_id=step.event.person_id,
                    at=step.event.at,
                    occupied=step.occupied_after,
                    capacity=self.capacity_of(cabin_id),
                    version=result.version,
                    message=f"manual correction {step.event.delta:+d}: {step.event.note}",
                    event_id=step.event.id,
                )
                if self._alarms.raise_alarm(alarm):
                    self._persist_alarm(alarm)
                    raised.append(alarm)
        return raised

    def _retroactive_over(
        self, alarm: Alarm, result: folding.FoldResult
    ) -> SwipeEvent | None:
        """Find the late/manual event responsible for an over-capacity level.

        Walks the replay up to the alarming step and returns the last applied
        non-online event whose delta pushed the count up. Returns ``None`` only
        when every contributing event was online (which the gate prevents).
        """

        responsible: SwipeEvent | None = None
        for step in result.steps:
            if step.event.id == alarm.event_id:
                break
            if not step.applied:
                continue
            if step.event.source != SOURCE_ONLINE and step.event.direction in (IN, ADJUST):
                delta = 1 if step.event.direction == IN else max(step.event.delta, 1)
                if delta > 0:
                    responsible = step.event
        trigger = self._ledger.get(alarm.event_id)
        if trigger is not None and trigger.source != SOURCE_ONLINE:
            responsible = trigger
        return responsible

    def _check_clock(self, event: SwipeEvent, sink: list[Alarm]) -> None:
        """Flag a device event stamped in the future (clock skew)."""

        if event.received_at and event.at > event.received_at:
            alarm = Alarm(
                key=f"clock_anomaly:{event.id}",
                kind="clock_anomaly",
                cabin_id=event.cabin_id,
                person_id=event.person_id,
                at=event.at,
                occupied=0,
                capacity=self.capacity_of(event.cabin_id),
                version=self._ledger.count(event.cabin_id),
                message=f"event time {event.at} is after arrival {event.received_at}",
                event_id=event.id,
            )
            if self._alarms.raise_alarm(alarm):
                self._persist_alarm(alarm)
                sink.append(alarm)

    def _dedupe_alarms(self, alarms: list[Alarm]) -> list[Alarm]:
        unique: list[Alarm] = []
        seen: set[str] = set()
        for alarm in alarms:
            if alarm.key not in seen:
                seen.add(alarm.key)
                unique.append(alarm)
        return unique

    def _persist_alarm(self, alarm: Alarm) -> None:
        if self._store is not None:
            from .persistence import save_alarm

            save_alarm(self._store, alarm)

    def _seconds_between(self, earlier: str, later: str) -> int:
        return self._stamp(later) - self._stamp(earlier)

    @staticmethod
    def _stamp(stamp: str) -> int:
        import calendar
        import time

        try:
            return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            digits = "".join(ch for ch in stamp if ch.isdigit())
            return int(digits[:14]) if len(digits) >= 14 else 0

    @staticmethod
    def _add_seconds(stamp: str, seconds: int) -> str:
        import time

        try:
            parsed = time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
            epoch = time.mktime(parsed)
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch + seconds))
        except ValueError:
            return stamp

    @staticmethod
    def _group_by_cabin(events: list[SwipeEvent]) -> dict[str, list[SwipeEvent]]:
        grouped: dict[str, list[SwipeEvent]] = {}
        for event in events:
            grouped.setdefault(event.cabin_id, []).append(event)
        return grouped

    def _recover(self) -> None:
        """Rebuild memory state from durable records on boot."""

        assert self._store is not None
        from .persistence import load_all_alarms, load_all_approvals, load_all_events, load_all_reservations

        for event in load_all_events(self._store):
            self._ledger.append(event)
        self._reservations.replace_all(load_all_reservations(self._store))
        self._alarms.replace_all(load_all_alarms(self._store))
        self._approvals = list(load_all_approvals(self._store))

    def describe(self) -> str:
        return (
            f"occupancy cabins={len(self.cabins())} events={self.ledger_count()} "
            f"alarms={self._alarms.count()}"
        )


class _Refold:
    """Small internal bundle returned after a cabin refold."""

    def __init__(
        self, occupied: int, version: int, alarms: list[Alarm], result: folding.FoldResult
    ) -> None:
        self.occupied = occupied
        self.version = version
        self.alarms = alarms
        self.result = result
