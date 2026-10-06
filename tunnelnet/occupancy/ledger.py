"""Occupancy ledger: one event log, one headcount, every consumer agrees.

The event log is the only fact.  Headcount and per-person presence are
materialised views updated in the same critical section that books the
event, and admissions, alerts and the console all quote the version of
that one view — so the console page can never disagree with the audit
trail.  Bootstrapping replays the durable log and verifies that every
persisted booking is reproduced exactly.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta

from tunnelnet.store import Store

from .model import Alert, CabinLimit, Decision, PassageEvent
from .order import DIRECTION_RANK, canon, now, order_key, parse

EVENT_KIND = "passage"
DECISION_KIND = "admission"
ALERT_KIND = "occ_alert"
LIMIT_KIND = "occ_limit"
SNAPSHOT_KIND = "occ_snapshot"

# An entry event arriving more than this many seconds after it happened is
# a backfill: re-check whether the cabin was already over capacity at the
# event's own moment.
LATE_SECONDS = 60


@dataclass
class _Presence:
    """Latest known state of one person in one cabin."""

    state: str  # "IN" | "OUT"
    key: tuple[str, int, str]  # order key of the newest applied event
    since: str  # event time of the entry that started the current stay


@dataclass
class _Count:
    """Materialised headcount of one cabin and its version."""

    value: int = 0
    version: int = 0


def _check_id(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} is required")
    if "/" in value or "\\" in value:
        raise ValueError(f"{name} must not contain path separators")


class OccupancyLedger:
    """Books passage events and gates admissions on the live headcount."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._events: dict[str, PassageEvent] = {}
        self._log: list[PassageEvent] = []
        self._last_event: dict[str, PassageEvent] = {}
        self._presence: dict[tuple[str, str], _Presence] = {}
        self._counts: dict[str, _Count] = {}
        self._limits: dict[str, CabinLimit] = {}
        self._decisions: dict[str, Decision] = {}
        self._alerts: dict[str, Alert] = {}
        self._device_seq: dict[str, int] = {}
        self._restore()

    # -- recovery ------------------------------------------------------

    def _restore(self) -> None:
        """Rebuild the materialised views by replaying the durable log."""

        for cabin_id in self._store.list(LIMIT_KIND):
            payload = self._store.load(LIMIT_KIND, cabin_id)
            if payload is not None:
                limit = CabinLimit.from_dict(payload)
                self._limits[limit.cabin_id] = limit
        for alert_id in self._store.list(ALERT_KIND):
            payload = self._store.load(ALERT_KIND, alert_id)
            if payload is not None:
                alert = Alert.from_dict(payload)
                self._alerts[alert.id] = alert
        for admission_id in self._store.list(DECISION_KIND):
            payload = self._store.load(DECISION_KIND, admission_id)
            if payload is not None:
                decision = Decision.from_dict(payload)
                self._decisions[decision.admission_id] = decision
        events: list[PassageEvent] = []
        for event_id in self._store.list(EVENT_KIND):
            payload = self._store.load(EVENT_KIND, event_id)
            if payload is not None:
                events.append(PassageEvent.from_dict(payload))
        events.sort(key=lambda event: event.seq)
        for event in events:
            self._book(event, persist=False, detect=False)
        # The durable snapshot may lag the log (a crash between the two
        # writes) but it must never run ahead of it.
        for cabin_id, count in self._counts.items():
            payload = self._store.load(SNAPSHOT_KIND, cabin_id)
            if payload is not None and int(payload.get("version", 0)) > count.version:  # type: ignore[arg-type]
                raise ValueError(f"corrupt occupancy snapshot for {cabin_id}")

    # -- configuration ---------------------------------------------------

    def configure(
        self,
        cabin_id: str,
        capacity: int | None = None,
        max_stay_minutes: int | None = None,
    ) -> CabinLimit:
        """Set the admission limit and maximum stay of one cabin."""

        if capacity is not None and capacity < 0:
            raise ValueError("capacity must not be negative")
        if max_stay_minutes is not None and max_stay_minutes <= 0:
            raise ValueError("max_stay_minutes must be positive")
        limit = CabinLimit(cabin_id=cabin_id, capacity=capacity, max_stay_minutes=max_stay_minutes)
        with self._lock:
            self._limits[cabin_id] = limit
            self._store.save(LIMIT_KIND, cabin_id, limit.as_dict())
        return limit

    def limit(self, cabin_id: str) -> CabinLimit | None:
        with self._lock:
            return self._limits.get(cabin_id)

    # -- booking events ----------------------------------------------------

    def apply_event(
        self,
        *,
        cabin_id: str,
        person_id: str,
        direction: str,
        event_time: str,
        event_id: str,
        source: str = "device",
        device_id: str = "",
        device_seq: int | None = None,
    ) -> dict[str, object]:
        """Book one passage event; replaying the same event_id is a no-op."""

        _check_id("event_id", event_id)
        _check_id("cabin_id", cabin_id)
        _check_id("person_id", person_id)
        if direction not in DIRECTION_RANK:
            raise ValueError("direction must be IN or OUT")
        stamp = canon(event_time)
        with self._lock:
            existing = self._events.get(event_id)
            if existing is not None:
                return self._result(existing, duplicate=True)
            event = PassageEvent(
                seq=len(self._log) + 1,
                event_id=event_id,
                cabin_id=cabin_id,
                person_id=person_id,
                direction=direction,
                event_time=stamp,
                received_at=now(),
                source=source,
                device_id=device_id,
                device_seq=device_seq,
                applied=False,
                count_after=0,
                version=0,
                anomaly="",
            )
            return self._book(event, persist=True, detect=True)

    def apply_batch(self, events: list[dict[str, object]]) -> list[dict[str, object]]:
        """Book each item independently; a failed item never blocks the rest.

        Offline devices replay the whole batch until every item reports
        ``applied``, ``recorded`` or ``duplicate`` — retries are idempotent
        because the event_id is the deduplication key.
        """

        results: list[dict[str, object]] = []
        for item in events:
            try:
                results.append(self.apply_event(**item))  # type: ignore[arg-type]
            except (KeyError, TypeError, ValueError) as exc:
                results.append(
                    {
                        "event_id": str(item.get("event_id", "")) if isinstance(item, dict) else "",
                        "status": "error",
                        "error": str(exc),
                    }
                )
        return results

    def _book(self, event: PassageEvent, *, persist: bool, detect: bool) -> dict[str, object]:
        """Apply one event to the views; the lock is already held."""

        key = (event.cabin_id, event.person_id)
        presence = self._presence.get(key)
        count = self._counts.setdefault(event.cabin_id, _Count())
        applied, anomaly, delta = True, "", 0
        state, since = "", ""

        if presence is None:
            if event.direction == "IN":
                state, since, delta = "IN", event.event_time, 1
            else:
                state, since, anomaly = "OUT", "", "exit_without_presence"
        else:
            new_key = order_key(event.event_time, event.direction, event.event_id)
            if new_key <= presence.key:
                # Late arrival: an older fact.  Book it for the audit
                # trail but leave the current state alone.
                applied, anomaly = False, "late_out_of_order"
                state, since = presence.state, presence.since
            elif event.direction == presence.state:
                # A person is a two-state machine, not a counter: the
                # repeated swipe does not move the headcount.
                anomaly = "double_entry" if event.direction == "IN" else "double_exit"
                state, since = presence.state, presence.since
            elif event.direction == "IN":
                state, since, delta = "IN", event.event_time, 1
            else:
                state, since, delta = "OUT", "", -1

        if applied:
            count.value += delta
            count.version += 1
            self._presence[key] = _Presence(
                state=state,
                key=order_key(event.event_time, event.direction, event.event_id),
                since=since,
            )

        booked = replace(
            event,
            applied=applied,
            anomaly=anomaly,
            count_after=count.value,
            version=count.version,
        )
        self._events[event.event_id] = booked
        self._log.append(booked)
        self._last_event[event.cabin_id] = booked

        if event.device_id and event.device_seq is not None:
            last = self._device_seq.get(event.device_id)
            if detect and last is not None and event.device_seq > last + 1:
                self._raise(
                    event.cabin_id,
                    "",
                    "device_gap",
                    count,
                    f"device {event.device_id} seq {last} -> {event.device_seq}: offline events may be missing",
                    dedupe_key=f"device_gap:{event.device_id}",
                    open_only=True,
                )
            if last is None or event.device_seq > last:
                self._device_seq[event.device_id] = event.device_seq

        if persist:
            self._store.save(EVENT_KIND, event.event_id, booked.as_dict())
            if applied:
                self._store.save(
                    SNAPSHOT_KIND,
                    event.cabin_id,
                    {
                        "cabin_id": event.cabin_id,
                        "in_count": count.value,
                        "version": count.version,
                    },
                )
        elif (
            booked.applied != event.applied
            or booked.anomaly != event.anomaly
            or booked.count_after != event.count_after
            or booked.version != event.version
        ):
            raise ValueError(f"corrupt occupancy log at {event.event_id}")

        if detect:
            self._detect(booked, count)
        return self._result(booked, duplicate=False)

    def _detect(self, event: PassageEvent, count: _Count) -> None:
        """Alerts raised by one freshly booked event."""

        limit = self._limits.get(event.cabin_id)
        if event.anomaly:
            self._raise(
                event.cabin_id,
                event.person_id,
                "anomaly",
                count,
                f"{event.anomaly}: {event.person_id} {event.direction} at {event.event_time}",
                dedupe_key=f"anomaly:{event.cabin_id}:{event.person_id}",
                open_only=True,
            )
        if event.direction != "IN" or not event.applied or limit is None or limit.capacity is None:
            return
        if count.value > limit.capacity:
            self._raise(
                event.cabin_id,
                event.person_id,
                "over_capacity",
                count,
                f"headcount {count.value} exceeds capacity {limit.capacity}",
                dedupe_key=f"over_capacity:{event.cabin_id}",
                open_only=True,
            )
        late = parse(event.received_at) - parse(event.event_time) > timedelta(seconds=LATE_SECONDS)
        if late:
            # A backfilled entry cannot undo past admissions, but it can
            # prove the cabin was actually over capacity back then.
            historical = self.replay(event.cabin_id, at=event.event_time)
            if historical > limit.capacity:
                self._raise(
                    event.cabin_id,
                    event.person_id,
                    "retro_conflict",
                    count,
                    f"backfill shows {historical} present at {event.event_time}, "
                    f"capacity {limit.capacity} was already exceeded",
                    dedupe_key=f"retro_conflict:{event.event_id}",
                    open_only=False,
                )

    # -- admissions --------------------------------------------------------

    def admit(self, *, cabin_id: str, person_id: str, admission_id: str = "") -> Decision:
        """Gate one admission on the live headcount and book the decision.

        The capacity check and the booking of the entry happen in one
        critical section, so concurrent swipes can never both win the last
        free slot.  Replaying the same admission_id returns the original
        decision.
        """

        _check_id("cabin_id", cabin_id)
        _check_id("person_id", person_id)
        with self._lock:
            admission_id = admission_id or uuid.uuid4().hex
            _check_id("admission_id", admission_id)
            existing = self._decisions.get(admission_id)
            if existing is not None:
                return existing
            count = self._counts.setdefault(cabin_id, _Count())
            limit = self._limits.get(cabin_id)
            denied = (
                limit is not None
                and limit.capacity is not None
                and count.value >= limit.capacity
            )
            decision = Decision(
                admission_id=admission_id,
                cabin_id=cabin_id,
                person_id=person_id,
                decision="DENIED" if denied else "APPROVED",
                reason="capacity" if denied else "",
                count_at_decision=count.value,
                occupancy_version=count.version,
                event_id="" if denied else f"adm:{admission_id}",
                decided_at=now(),
            )
            self._decisions[admission_id] = decision
            self._store.save(DECISION_KIND, admission_id, decision.as_dict())
            if not denied:
                self.apply_event(
                    cabin_id=cabin_id,
                    person_id=person_id,
                    direction="IN",
                    event_time=now(),
                    event_id=decision.event_id,
                    source="approval",
                )
            return decision

    def decisions(self, cabin_id: str | None = None) -> list[Decision]:
        with self._lock:
            return [
                decision
                for decision in sorted(self._decisions.values(), key=lambda item: item.decided_at)
                if cabin_id is None or decision.cabin_id == cabin_id
            ]

    # -- scheduled scans -----------------------------------------------------

    def scan_overstay(self, at: str | None = None) -> int:
        """Raise one alert per stay that exceeded the cabin's maximum stay."""

        moment = parse(canon(at)) if at else parse(now())
        raised = 0
        with self._lock:
            for (cabin_id, person_id), presence in list(self._presence.items()):
                if presence.state != "IN":
                    continue
                limit = self._limits.get(cabin_id)
                if limit is None or limit.max_stay_minutes is None:
                    continue
                deadline = parse(presence.since) + timedelta(minutes=limit.max_stay_minutes)
                if moment > deadline:
                    count = self._counts[cabin_id]
                    alert = self._raise(
                        cabin_id,
                        person_id,
                        "overstay",
                        count,
                        f"{person_id} inside since {presence.since}, "
                        f"limit {limit.max_stay_minutes} minutes",
                        dedupe_key=f"overstay:{cabin_id}:{person_id}:{presence.since}",
                        open_only=False,
                    )
                    if alert is not None:
                        raised += 1
        return raised

    # -- reading the views ---------------------------------------------------

    def replay(self, cabin_id: str, at: str | None = None) -> int:
        """Headcount rebuilt from the log alone: each person's newest event wins."""

        with self._lock:
            latest: dict[str, tuple[tuple[str, int, str], str]] = {}
            for event in self._log:
                if event.cabin_id != cabin_id:
                    continue
                if at is not None and event.event_time > at:
                    continue
                key = order_key(event.event_time, event.direction, event.event_id)
                current = latest.get(event.person_id)
                if current is None or key > current[0]:
                    latest[event.person_id] = (key, event.direction)
            return sum(1 for _, direction in latest.values() if direction == "IN")

    def console(self, cabin_id: str, at: str | None = None) -> dict[str, object]:
        """The console view, cross-checked against the audit log on every read."""

        with self._lock:
            count = self._counts.get(cabin_id, _Count())
            limit = self._limits.get(cabin_id)
            last = self._last_event.get(cabin_id)
            ledger_count = last.count_after if last is not None else 0
            replayed = self.replay(cabin_id)
            moment = parse(canon(at)) if at else parse(now())
            roster = []
            for (cabin, person_id), presence in self._presence.items():
                if cabin != cabin_id or presence.state != "IN":
                    continue
                stayed = int((moment - parse(presence.since)).total_seconds())
                overstayed = (
                    limit is not None
                    and limit.max_stay_minutes is not None
                    and stayed > limit.max_stay_minutes * 60
                )
                roster.append(
                    {
                        "person_id": person_id,
                        "since": presence.since,
                        "stayed_seconds": stayed,
                        "overstayed": overstayed,
                    }
                )
            roster.sort(key=lambda row: row["since"])  # type: ignore[index]
            capacity = limit.capacity if limit is not None else None
            return {
                "cabin_id": cabin_id,
                "capacity": capacity,
                "max_stay_minutes": limit.max_stay_minutes if limit is not None else None,
                "in_count": count.value,
                "ledger_count": ledger_count,
                "replay_count": replayed,
                "consistent": count.value == ledger_count == replayed,
                "version": count.version,
                "free_slots": None if capacity is None else max(0, capacity - count.value),
                "roster": roster,
                "open_alerts": [
                    alert.as_dict() for alert in self.alerts(cabin_id=cabin_id)
                ],
            }

    def reconcile(self, cabin_id: str) -> dict[str, object]:
        """Verify the materialised count against the log; alert on divergence."""

        with self._lock:
            view = self.console(cabin_id)
            if not view["consistent"]:
                count = self._counts.get(cabin_id, _Count())
                self._raise(
                    cabin_id,
                    "",
                    "reconcile_mismatch",
                    count,
                    f"materialised {view['in_count']} != ledger {view['ledger_count']} "
                    f"!= replay {view['replay_count']}",
                    dedupe_key=f"reconcile_mismatch:{cabin_id}",
                    open_only=True,
                )
                view = self.console(cabin_id)
            return view

    def overview(self) -> list[dict[str, object]]:
        """Console view of every cabin that has a limit or a headcount."""

        with self._lock:
            cabin_ids = sorted(set(self._limits) | set(self._counts))
            return [self.console(cabin_id) for cabin_id in cabin_ids]

    def audit(self, cabin_id: str | None = None) -> list[PassageEvent]:
        """The passage log in arrival order — the audit trail itself."""

        with self._lock:
            return [
                event
                for event in self._log
                if cabin_id is None or event.cabin_id == cabin_id
            ]

    def alerts(self, cabin_id: str | None = None, status: str = "open") -> list[Alert]:
        with self._lock:
            return [
                alert
                for alert in sorted(self._alerts.values(), key=lambda item: item.created_at)
                if (cabin_id is None or alert.cabin_id == cabin_id) and alert.status == status
            ]

    def ack_alert(self, alert_id: str) -> Alert:
        with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is None:
                raise ValueError("alert not found")
            acked = replace(alert, status="acked")
            self._alerts[alert_id] = acked
            self._store.save(ALERT_KIND, alert_id, acked.as_dict())
            return acked

    def state(self) -> dict[str, object]:
        with self._lock:
            return {
                "cabins": {
                    cabin_id: {"in_count": count.value, "version": count.version}
                    for cabin_id, count in sorted(self._counts.items())
                },
                "events": len(self._log),
                "decisions": len(self._decisions),
                "open_alerts": len(self.alerts()),
            }

    def describe(self) -> str:
        with self._lock:
            return (
                f"occupancy cabins={len(self._counts)} events={len(self._log)} "
                f"open_alerts={len(self.alerts())}"
            )

    # -- internals -----------------------------------------------------------

    def _raise(
        self,
        cabin_id: str,
        person_id: str,
        type_: str,
        count: _Count,
        detail: str,
        *,
        dedupe_key: str,
        open_only: bool,
    ) -> Alert | None:
        """Create an alert unless its dedupe key already has one."""

        for alert in self._alerts.values():
            if alert.dedupe_key == dedupe_key and (not open_only or alert.status == "open"):
                return None
        alert = Alert(
            id=uuid.uuid4().hex,
            cabin_id=cabin_id,
            person_id=person_id,
            type=type_,
            status="open",
            dedupe_key=dedupe_key,
            count_at_alert=count.value,
            occupancy_version=count.version,
            detail=detail,
            created_at=now(),
        )
        self._alerts[alert.id] = alert
        self._store.save(ALERT_KIND, alert.id, alert.as_dict())
        return alert

    @staticmethod
    def _result(event: PassageEvent, duplicate: bool) -> dict[str, object]:
        if duplicate:
            status = "duplicate"
        elif event.applied:
            status = "applied"
        else:
            status = "recorded"
        return {
            "event_id": event.event_id,
            "status": status,
            "applied": event.applied,
            "anomaly": event.anomaly,
            "count_after": event.count_after,
            "occupancy_version": event.version,
        }
