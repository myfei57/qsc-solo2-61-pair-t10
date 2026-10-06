"""Append only swipe ledger with idempotent ingestion and per cabin locks.

The ledger is the system of record. Events are never deleted or rewritten; a
late/backfilled event is simply appended and the fold replayed. Every event id
is accepted at most once, which is what makes offline replay and at-least-once
device delivery idempotent: resending a batch yields the same ledger state.

Mutations for one cabin are serialised by one lock, so concurrent card reads
against the same cabin observe a single consistent head count. Different
cabins use different locks and never block each other.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from .model import (
    ADJUST,
    DIRECTIONS,
    IN,
    OUT,
    SOURCES,
    SwipeEvent,
    is_direction,
)


class EventLedger:
    """Holds every accepted event grouped by cabin in append order."""

    def __init__(self) -> None:
        self._events: dict[str, list[SwipeEvent]] = {}
        self._ids: dict[str, str] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    def lock_for(self, cabin_id: str) -> threading.RLock:
        """Return the serialisation lock for one cabin, creating it on demand."""

        with self._guard:
            lock = self._locks.get(cabin_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[cabin_id] = lock
            return lock

    def has(self, event_id: str) -> bool:
        with self._guard:
            return event_id in self._ids

    def get(self, event_id: str) -> SwipeEvent | None:
        with self._guard:
            for events in self._events.values():
                for event in events:
                    if event.id == event_id:
                        return event
        return None

    def append(self, event: SwipeEvent) -> bool:
        """Append one event. Returns ``False`` when its id was already seen."""

        validate_event(event)
        with self._guard:
            if event.id in self._ids:
                return False
            self._ids[event.id] = event.cabin_id
            self._events.setdefault(event.cabin_id, []).append(event)
        return True

    def extend(self, events: Iterable[SwipeEvent]) -> tuple[list[SwipeEvent], list[SwipeEvent]]:
        """Append many events, returning ``(accepted, duplicates)``.

        Duplicate ids are skipped silently; the accepted list preserves input
        order so an offline batch can be validated event by event by the
        caller before append.
        """

        accepted: list[SwipeEvent] = []
        duplicates: list[SwipeEvent] = []
        for event in events:
            validate_event(event)
            with self._guard:
                if event.id in self._ids:
                    duplicates.append(event)
                    continue
                self._ids[event.id] = event.cabin_id
                self._events.setdefault(event.cabin_id, []).append(event)
                accepted.append(event)
        return accepted, duplicates

    def events(self, cabin_id: str) -> list[SwipeEvent]:
        with self._guard:
            return list(self._events.get(cabin_id, ()))

    def all_events(self) -> list[SwipeEvent]:
        with self._guard:
            return [event for events in self._events.values() for event in events]

    def cabins(self) -> list[str]:
        with self._guard:
            return sorted(self._events)

    def count(self, cabin_id: str | None = None) -> int:
        with self._guard:
            if cabin_id is not None:
                return len(self._events.get(cabin_id, ()))
            return sum(len(events) for events in self._events.values())

    def replace_all(self, events: Iterable[SwipeEvent]) -> None:
        """Rebuild the ledger from durable records during boot/recovery."""

        with self._guard:
            self._events.clear()
            self._ids.clear()
            for event in events:
                self._ids[event.id] = event.cabin_id
                self._events.setdefault(event.cabin_id, []).append(event)

    def synchronized(self, cabin_id: str, action: Callable[[], object]) -> object:
        """Run ``action`` while holding the cabin serialisation lock."""

        with self.lock_for(cabin_id):
            return action()


def validate_event(event: SwipeEvent) -> None:
    """Reject malformed events before they can touch the immutable log."""

    if not event.id:
        raise ValueError("event id is empty")
    if not event.cabin_id:
        raise ValueError("cabin_id is empty")
    if not event.person_id:
        raise ValueError("person_id is empty")
    if not is_direction(event.direction):
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if not event.at:
        raise ValueError("event time at is empty")
    if event.source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if event.direction == ADJUST and event.delta == 0:
        raise ValueError("adjust event needs a non-zero delta")
    if event.direction in (IN, OUT) and event.delta:
        raise ValueError("in/out events carry no delta")


def same_direction(first: SwipeEvent, second: SwipeEvent) -> bool:
    return (
        first.cabin_id == second.cabin_id
        and first.person_id == second.person_id
        and first.direction == second.direction
    )
