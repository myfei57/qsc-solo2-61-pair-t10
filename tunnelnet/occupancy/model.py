"""Records kept by the occupancy ledger."""

from __future__ import annotations

from dataclasses import dataclass


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CabinLimit:
    """Admission limit and maximum stay configured for one cabin."""

    cabin_id: str
    capacity: int | None = None  # None = count only, never block admissions
    max_stay_minutes: int | None = None  # None = no overstay detection

    def as_dict(self) -> dict[str, object]:
        return {
            "cabin_id": self.cabin_id,
            "capacity": self.capacity,
            "max_stay_minutes": self.max_stay_minutes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CabinLimit":
        return cls(
            cabin_id=str(payload.get("cabin_id", "")),
            capacity=_optional_int(payload.get("capacity")),
            max_stay_minutes=_optional_int(payload.get("max_stay_minutes")),
        )


@dataclass(frozen=True)
class PassageEvent:
    """One entry or exit fact; the atom of the occupancy audit trail."""

    seq: int  # arrival order on the server, the audit sequence
    event_id: str  # idempotency key supplied by the caller
    cabin_id: str
    person_id: str
    direction: str  # "IN" | "OUT"
    event_time: str  # canonical event time, used for attribution
    received_at: str  # server arrival time, used only for lateness detection
    source: str  # "device" | "backfill" | "approval"
    device_id: str
    device_seq: int | None
    applied: bool  # False = arrived late, booked without changing the state
    count_after: int  # headcount right after this event was booked
    version: int  # occupancy version right after this event was booked
    anomaly: str  # "" or double_entry/double_exit/exit_without_presence/late_out_of_order

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "direction": self.direction,
            "event_time": self.event_time,
            "received_at": self.received_at,
            "source": self.source,
            "device_id": self.device_id,
            "device_seq": self.device_seq,
            "applied": self.applied,
            "count_after": self.count_after,
            "version": self.version,
            "anomaly": self.anomaly,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PassageEvent":
        return cls(
            seq=int(payload.get("seq", 0)),  # type: ignore[arg-type]
            event_id=str(payload.get("event_id", "")),
            cabin_id=str(payload.get("cabin_id", "")),
            person_id=str(payload.get("person_id", "")),
            direction=str(payload.get("direction", "")),
            event_time=str(payload.get("event_time", "")),
            received_at=str(payload.get("received_at", "")),
            source=str(payload.get("source", "device")),
            device_id=str(payload.get("device_id", "")),
            device_seq=_optional_int(payload.get("device_seq")),
            applied=bool(payload.get("applied", False)),
            count_after=int(payload.get("count_after", 0)),  # type: ignore[arg-type]
            version=int(payload.get("version", 0)),  # type: ignore[arg-type]
            anomaly=str(payload.get("anomaly", "")),
        )


@dataclass(frozen=True)
class Decision:
    """One admission decision; approvals and denials are both audit records."""

    admission_id: str  # idempotency key supplied by the caller
    cabin_id: str
    person_id: str
    decision: str  # "APPROVED" | "DENIED"
    reason: str  # "" or "capacity"
    count_at_decision: int  # headcount the gate saw when it decided
    occupancy_version: int  # ledger version the decision refers to
    event_id: str  # entry event booked on approval, "" when denied
    decided_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "decision": self.decision,
            "reason": self.reason,
            "count_at_decision": self.count_at_decision,
            "occupancy_version": self.occupancy_version,
            "event_id": self.event_id,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Decision":
        return cls(
            admission_id=str(payload.get("admission_id", "")),
            cabin_id=str(payload.get("cabin_id", "")),
            person_id=str(payload.get("person_id", "")),
            decision=str(payload.get("decision", "")),
            reason=str(payload.get("reason", "")),
            count_at_decision=int(payload.get("count_at_decision", 0)),  # type: ignore[arg-type]
            occupancy_version=int(payload.get("occupancy_version", 0)),  # type: ignore[arg-type]
            event_id=str(payload.get("event_id", "")),
            decided_at=str(payload.get("decided_at", "")),
        )


@dataclass(frozen=True)
class Alert:
    """One occupancy alert; deduplicated by its dedupe_key."""

    id: str
    cabin_id: str
    person_id: str
    type: str  # over_capacity/overstay/anomaly/retro_conflict/device_gap/reconcile_mismatch
    status: str  # "open" | "acked"
    dedupe_key: str
    count_at_alert: int
    occupancy_version: int
    detail: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cabin_id": self.cabin_id,
            "person_id": self.person_id,
            "type": self.type,
            "status": self.status,
            "dedupe_key": self.dedupe_key,
            "count_at_alert": self.count_at_alert,
            "occupancy_version": self.occupancy_version,
            "detail": self.detail,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Alert":
        return cls(
            id=str(payload.get("id", "")),
            cabin_id=str(payload.get("cabin_id", "")),
            person_id=str(payload.get("person_id", "")),
            type=str(payload.get("type", "")),
            status=str(payload.get("status", "open")),
            dedupe_key=str(payload.get("dedupe_key", "")),
            count_at_alert=int(payload.get("count_at_alert", 0)),  # type: ignore[arg-type]
            occupancy_version=int(payload.get("occupancy_version", 0)),  # type: ignore[arg-type]
            detail=str(payload.get("detail", "")),
            created_at=str(payload.get("created_at", "")),
        )
