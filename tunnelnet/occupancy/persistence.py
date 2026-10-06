"""Durable occupancy records kept beside the other control documents.

Three kinds are persisted in the shared file store:

* ``occ-event``       every accepted swipe/adjust event (the system of record)
* ``occ-reservation`` approved but possibly unconsumed seat reservations
* ``occ-alarm``       raised alarms, so acknowledgements survive a restart

On boot the ledger is rebuilt by re-folding these records, which is also why
event ids must be globally unique: the durable append is the idempotency
boundary for device retries after a server restart.
"""

from __future__ import annotations

from ..store import Store
from .model import Alarm, ApprovalRecord, Reservation, SwipeEvent

EVENT_KIND = "occ-event"
RESERVATION_KIND = "occ-reservation"
ALARM_KIND = "occ-alarm"
APPROVAL_KIND = "occ-approval"


def save_event(store: Store, event: SwipeEvent) -> None:
    store.save(EVENT_KIND, event.id, event.as_dict())


def load_all_events(store: Store) -> list[SwipeEvent]:
    events: list[SwipeEvent] = []
    for event_id in store.list(EVENT_KIND):
        payload = store.load(EVENT_KIND, event_id)
        if payload is not None:
            events.append(SwipeEvent.from_dict(payload))
    return events


def event_exists(store: Store, event_id: str) -> bool:
    return store.exists(EVENT_KIND, event_id)


def save_reservation(store: Store, reservation: Reservation) -> None:
    store.save(RESERVATION_KIND, reservation.id, reservation.as_dict())


def load_all_reservations(store: Store) -> list[Reservation]:
    reservations: list[Reservation] = []
    for reservation_id in store.list(RESERVATION_KIND):
        payload = store.load(RESERVATION_KIND, reservation_id)
        if payload is not None:
            reservations.append(
                Reservation(
                    id=str(payload.get("id", "")),
                    cabin_id=str(payload.get("cabin_id", "")),
                    person_id=str(payload.get("person_id", "")),
                    created_at=str(payload.get("created_at", "")),
                    expires_at=str(payload.get("expires_at", "")),
                    consumed_by=str(payload.get("consumed_by", "")),
                )
            )
    return reservations


def save_approval(store: Store, record: ApprovalRecord) -> None:
    store.save(APPROVAL_KIND, record.id, record.as_dict())


def load_all_approvals(store: Store) -> list[ApprovalRecord]:
    records: list[ApprovalRecord] = []
    for record_id in store.list(APPROVAL_KIND):
        payload = store.load(APPROVAL_KIND, record_id)
        if payload is not None:
            records.append(
                ApprovalRecord(
                    id=str(payload.get("id", "")),
                    cabin_id=str(payload.get("cabin_id", "")),
                    person_id=str(payload.get("person_id", "")),
                    allowed=bool(payload.get("allowed", False)),
                    occupied=int(payload.get("occupied", 0)),
                    capacity=int(payload.get("capacity", 0)),
                    reserved=int(payload.get("reserved", 0)),
                    version=int(payload.get("version", 0)),
                    at=str(payload.get("at", "")),
                    reason=str(payload.get("reason", "")),
                    reservation_id=str(payload.get("reservation_id", "")),
                )
            )
    return records


def save_alarm(store: Store, alarm: Alarm) -> None:
    store.save(ALARM_KIND, alarm.key, alarm.as_dict())


def load_all_alarms(store: Store) -> list[Alarm]:
    alarms: list[Alarm] = []
    for key in store.list(ALARM_KIND):
        payload = store.load(ALARM_KIND, key)
        if payload is not None:
            alarms.append(
                Alarm(
                    key=str(payload.get("key", "")),
                    kind=str(payload.get("kind", "")),
                    cabin_id=str(payload.get("cabin_id", "")),
                    person_id=str(payload.get("person_id", "")),
                    at=str(payload.get("at", "")),
                    occupied=int(payload.get("occupied", 0)),
                    capacity=int(payload.get("capacity", 0)),
                    version=int(payload.get("version", 0)),
                    message=str(payload.get("message", "")),
                    event_id=str(payload.get("event_id", "")),
                    active=bool(payload.get("active", True)),
                )
            )
    return alarms
