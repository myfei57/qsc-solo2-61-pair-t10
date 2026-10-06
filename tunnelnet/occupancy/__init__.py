"""Per-cabin headcount ledger and capacity-gated admission.

Design rulings this package commits to:

D1  Attribution is by *event time*.  Who was inside at moment T is derived
    from the events' own timestamps; the server arrival time is kept only
    to detect lateness.  A late event is booked into the audit trail but
    does not rewrite the current state unless it is the person's newest
    fact.

D2  Decisions are immutable.  An admission decision quotes the headcount
    and occupancy version it saw; a backfilled event never undoes a past
    release, it raises a retrospective ``retro_conflict`` alert instead.

D3  A person is a two-state machine (IN/OUT), not a counter.  A repeated
    same-direction swipe does not move the headcount (``double_entry`` /
    ``double_exit`` anomaly).  An out/in pair is ordered by
    ``(event_time, exit-before-entry, event_id)`` — at identical event
    times the exit is deducted first and the pair settles on IN, the
    fail-safe side for both headcount and capacity.

D4  One source of truth.  The event log is the only fact; headcount and
    presence are materialised views updated in the same critical section
    that books the event.  Every event, decision and alert quotes the
    occupancy version, and the console view re-verifies
    ``materialised == last log booking == full replay`` on every read.

D5  Idempotency.  ``event_id`` and ``admission_id`` are idempotency keys;
    replays return the original booking.  Batch uploads book each item in
    its own critical section and report per-item status, so a partially
    failed backfill is retried item by item.

D6  Concurrency.  One lock serialises every check-and-book section, so
    the capacity check and the entry it admits cannot interleave with
    another swipe.  A storage engine with per-cabin row locks can replace
    the single lock without changing the rules above.
"""

from .ledger import OccupancyLedger
from .model import Alert, CabinLimit, Decision, PassageEvent
from .order import DIRECTION_RANK, canon, now, order_key

__all__ = [
    "Alert",
    "CabinLimit",
    "DIRECTION_RANK",
    "Decision",
    "OccupancyLedger",
    "PassageEvent",
    "canon",
    "now",
    "order_key",
]
