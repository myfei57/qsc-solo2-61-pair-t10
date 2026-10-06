"""Ordering and timestamp rules shared by every occupancy decision.

Presence is attributed by *event time* (the moment the device says the
person passed), never by arrival time; arrival order only shows up in the
audit sequence number.  To make that rule enforceable, every timestamp is
canonicalised to a fixed-width UTC string so plain string comparison is
also chronological comparison.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Tie-break rank for identical event times: an exit is ordered before an
# entry ("deduct first, then add"), so a same-moment out/in pair always
# settles on IN — the fail-safe side for both headcount and capacity.
DIRECTION_RANK = {"OUT": 0, "IN": 1}


def canon(stamp: str) -> str:
    """Normalise an ISO-8601 timestamp to sortable fixed-width UTC."""

    text = stamp.strip()
    if not text:
        raise ValueError("timestamp is empty")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def now() -> str:
    """Canonical timestamp of the current moment."""

    return canon(datetime.now(timezone.utc).isoformat())


def parse(stamp: str) -> datetime:
    """Parse a canonical timestamp back into an aware datetime."""

    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def order_key(stamp: str, direction: str, event_id: str) -> tuple[str, int, str]:
    """Total order over one person's events: event time, exit-first, id."""

    return (stamp, DIRECTION_RANK[direction], event_id)
