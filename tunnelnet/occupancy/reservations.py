"""Seat reservations created by approvals before the card is swiped.

A reservation holds one unit of capacity so that an approved person walking to
the door cannot be overtaken by someone else while the head count is at the
limit. It is consumed by that person's next IN and released by an expiry sweep
when the approval window passes unused.
"""

from __future__ import annotations

from .model import Reservation


class ReservationBook:
    """Per cabin reservations with active/expired/consumed lifecycle."""

    def __init__(self) -> None:
        self._items: dict[str, Reservation] = {}

    def add(self, reservation: Reservation) -> Reservation:
        if not reservation.id:
            raise ValueError("reservation id is empty")
        self._items[reservation.id] = reservation
        return reservation

    def get(self, reservation_id: str) -> Reservation | None:
        return self._items.get(reservation_id)

    def consume(self, reservation_id: str, event_id: str) -> Reservation | None:
        reservation = self._items.get(reservation_id)
        if reservation is None or reservation.consumed_by:
            return None
        consumed = Reservation(
            id=reservation.id,
            cabin_id=reservation.cabin_id,
            person_id=reservation.person_id,
            created_at=reservation.created_at,
            expires_at=reservation.expires_at,
            consumed_by=event_id,
        )
        self._items[reservation_id] = consumed
        return consumed

    def active(self, cabin_id: str, now: str = "") -> list[Reservation]:
        return [
            item
            for item in self._items.values()
            if item.cabin_id == cabin_id and item.active() and (not now or not self._expired(item, now))
        ]

    def active_count(self, cabin_id: str, now: str = "") -> int:
        return len(self.active(cabin_id, now))

    def find_for_person(self, cabin_id: str, person_id: str, now: str = "") -> Reservation | None:
        """Oldest active reservation for one person, which the next IN consumes."""

        candidates = [
            item
            for item in self.active(cabin_id, now)
            if item.person_id == person_id
        ]
        return min(candidates, key=lambda item: item.created_at, default=None)

    def sweep(self, now: str) -> list[Reservation]:
        """Expire reservations whose window has passed; returns the expired ones."""

        expired: list[Reservation] = []
        for item in list(self._items.values()):
            if item.active() and self._expired(item, now):
                expired.append(item)
        return expired

    @staticmethod
    def _expired(reservation: Reservation, now: str) -> bool:
        return bool(reservation.expires_at) and reservation.expires_at < now

    def all(self) -> list[Reservation]:
        return [self._items[key] for key in sorted(self._items)]

    def replace_all(self, reservations: list[Reservation]) -> None:
        self._items = {item.id: item for item in reservations}
