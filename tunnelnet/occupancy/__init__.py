"""Cabin occupancy counting, capacity admission and occupancy alarms.

The package turns door card reads into one auditable per-cabin head count and
blocks new releases once a cabin is full. One immutable swipe event log is the
single source of truth: the console head count, release approvals and alarms
are all produced by folding that same log.

Attribution is by device event time (OUT before IN on an exact timestamp tie);
late/backfilled events append and re-fold with retroactive alarms instead of
rewriting past decisions. Swipes follow a per-person state machine rather than
"first subtract then add", and offline batches are idempotent partial
failures.
"""

from .alarms import AlarmRegistry
from .fold import (
    FoldResult,
    FoldStep,
    alarms_from_violations,
    canonical,
    dwell_seconds,
    fold,
    fold_as_of,
    present_people,
)
from .ledger import EventLedger, same_direction, validate_event
from .model import (
    ADJUST,
    IN,
    OUT,
    AdmissionDecision,
    Alarm,
    ApprovalRecord,
    IngestResult,
    RejectedEvent,
    Reservation,
    SwipeEvent,
)
from .report import CabinOccupancy, OccupancyState, cabin_row, collect
from .reservations import ReservationBook
from .service import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    DEFAULT_DWELL_LIMIT_SECONDS,
    DEFAULT_LATE_WINDOW_SECONDS,
    OccupancyService,
)

__all__ = [
    "ADJUST",
    "IN",
    "OUT",
    "AdmissionDecision",
    "Alarm",
    "AlarmRegistry",
    "ApprovalRecord",
    "CabinOccupancy",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "DEFAULT_DWELL_LIMIT_SECONDS",
    "DEFAULT_LATE_WINDOW_SECONDS",
    "EventLedger",
    "FoldResult",
    "FoldStep",
    "IngestResult",
    "OccupancyService",
    "OccupancyState",
    "RejectedEvent",
    "Reservation",
    "ReservationBook",
    "SwipeEvent",
    "alarms_from_violations",
    "cabin_row",
    "canonical",
    "collect",
    "dwell_seconds",
    "fold",
    "fold_as_of",
    "present_people",
    "same_direction",
    "validate_event",
]
