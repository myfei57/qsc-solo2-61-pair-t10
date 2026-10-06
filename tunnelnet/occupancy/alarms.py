"""De-duplicating occupancy alarm registry.

Alarms carry the head count and ledger version they were raised against, so an
alarm row and the console can always be checked against the same fold. Folding
the immutable log repeatedly produces the same violation event ids, which are
used as stable de-duplication keys: re-folding after a late event never raises
a second alarm for a violation already known.
"""

from __future__ import annotations

from .model import Alarm


class AlarmRegistry:
    """Holds every occupancy alarm keyed by a stable de-duplication key."""

    def __init__(self) -> None:
        self._items: dict[str, Alarm] = {}

    def raise_alarm(self, alarm: Alarm) -> bool:
        """Record an alarm unless its key is already known. Returns whether new."""

        if alarm.key in self._items:
            return False
        self._items[alarm.key] = alarm
        return True

    def raise_many(self, alarms: list[Alarm]) -> list[Alarm]:
        return [alarm for alarm in alarms if self.raise_alarm(alarm)]

    def has(self, key: str) -> bool:
        return key in self._items

    def get(self, key: str) -> Alarm | None:
        return self._items.get(key)

    def all(self) -> list[Alarm]:
        return [self._items[key] for key in sorted(self._items)]

    def for_cabin(self, cabin_id: str) -> list[Alarm]:
        return [alarm for alarm in self.all() if alarm.cabin_id == cabin_id]

    def active(self, cabin_id: str = "") -> list[Alarm]:
        return [
            alarm
            for alarm in self.all()
            if alarm.active and (not cabin_id or alarm.cabin_id == cabin_id)
        ]

    def clear(self, key: str) -> bool:
        """Acknowledge one alarm; returns whether it existed."""

        alarm = self._items.get(key)
        if alarm is None:
            return False
        self._items[key] = Alarm(**{**alarm.__dict__, "active": False})
        return True

    def clear_cabin(self, cabin_id: str) -> int:
        count = 0
        for alarm in self.all():
            if alarm.cabin_id == cabin_id and alarm.active and self.clear(alarm.key):
                count += 1
        return count

    def count(self, cabin_id: str = "") -> int:
        return len(self.active(cabin_id))

    def replace_all(self, alarms: list[Alarm]) -> None:
        self._items = {alarm.key: alarm for alarm in alarms}
