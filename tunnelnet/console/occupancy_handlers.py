"""HTTP handlers for occupancy head counting, admission and alarms."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from tunnelnet.occupancy import SwipeEvent, collect
from tunnelnet.occupancy.model import SOURCE_BACKFILL

from .audit_log import record_event
from .http import Request, RequestError, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _service(server: "Server"):
    return server.runtime.occupancy


def occupancy_overview(server: "Server", request: Request) -> Response:
    return json_response(collect(_service(server)).as_dict())


def occupancy_cabins(server: "Server", request: Request) -> Response:
    service = _service(server)
    state = collect(service)
    return json_response({"cabins": [row.as_dict() for row in state.cabins]})


def occupancy_cabin(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    if not cabin_id:
        raise RequestError(400, "cabin_id is required")
    state = collect(service)
    row = state.cabin(cabin_id)
    if row is None:
        raise RequestError(404, f"cabin {cabin_id} has no occupancy configuration")
    return json_response(row.as_dict())


def occupancy_configure(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    capacity = request.int_field("capacity")
    if not cabin_id:
        raise RequestError(400, "cabin_id is required")
    if capacity <= 0:
        raise RequestError(400, "capacity must be positive")
    dwell = request.int_field("dwell_limit_seconds", 0)
    service.configure_cabin(cabin_id, capacity, dwell_limit_seconds=dwell)
    record_event(server.runtime, "occupancy_configure", cabin_id)
    return json_response({"configured": True, "cabin_id": cabin_id, "capacity": capacity})


def occupancy_swipe(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    person_id = request.str_field("person_id")
    direction = request.str_field("direction")
    if not cabin_id or not person_id or direction not in ("in", "out"):
        raise RequestError(400, "cabin_id, person_id and direction(in|out) are required")
    result = service.swipe(
        cabin_id,
        person_id,
        direction,
        event_id=request.str_field("event_id") or uuid.uuid4().hex,
        at=request.str_field("at"),
        seq=request.int_field("seq", 0),
        device_id=request.str_field("device_id"),
    )
    record_event(server.runtime, "occupancy_swipe", cabin_id)
    return json_response(result.as_dict())


def occupancy_backfill(server: "Server", request: Request) -> Response:
    """Ingest one offline/backfilled swipe (idempotent by event_id)."""

    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    person_id = request.str_field("person_id")
    direction = request.str_field("direction")
    at = request.str_field("at")
    if not cabin_id or not person_id or direction not in ("in", "out") or not at:
        raise RequestError(400, "cabin_id, person_id, direction(in|out) and at are required")
    result = service.backfill(
        cabin_id,
        person_id,
        direction,
        at,
        event_id=request.str_field("event_id") or uuid.uuid4().hex,
        seq=request.int_field("seq", 0),
        device_id=request.str_field("device_id"),
    )
    record_event(server.runtime, "occupancy_backfill", cabin_id)
    return json_response(result.as_dict())


def occupancy_batch(server: "Server", request: Request) -> Response:
    """Ingest an offline batch; bad events are rejected, the rest apply."""

    service = _service(server)
    raw = request.payload.get("events", [])
    if not isinstance(raw, list) or not raw:
        raise RequestError(400, "events must be a non-empty list")
    events: list[SwipeEvent] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RequestError(400, "every event must be an object")
        events.append(
            SwipeEvent(
                id=str(item.get("event_id") or item.get("id") or uuid.uuid4().hex),
                cabin_id=str(item.get("cabin_id", "")),
                person_id=str(item.get("person_id", "")),
                direction=str(item.get("direction", "")),
                at=str(item.get("at", "")),
                seq=int(item.get("seq", 0) or 0),
                device_id=str(item.get("device_id", "")),
                received_at=str(item.get("received_at", "")),
                source=str(item.get("source", SOURCE_BACKFILL)),
            )
        )
    result = service.ingest_batch(events)
    record_event(server.runtime, "occupancy_batch")
    return json_response(result.as_dict())


def occupancy_approve(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    person_id = request.str_field("person_id")
    if not cabin_id or not person_id:
        raise RequestError(400, "cabin_id and person_id are required")
    record = service.approve(
        cabin_id,
        person_id,
        ttl_seconds=request.int_field("ttl_seconds", 600),
        approval_id=request.str_field("approval_id"),
        at=request.str_field("at"),
    )
    record_event(server.runtime, "occupancy_approve", cabin_id)
    return json_response(record.as_dict())


def occupancy_approvals(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    return json_response({"approvals": [item.as_dict() for item in service.approvals(cabin_id)]})


def occupancy_reservations(server: "Server", request: Request) -> Response:
    service = _service(server)
    return json_response({"reservations": [item.as_dict() for item in service.reservations()]})


def occupancy_adjust(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    delta = request.int_field("delta")
    note = request.str_field("note", "manual adjustment")
    if not cabin_id or delta == 0:
        raise RequestError(400, "cabin_id and a non-zero delta are required")
    result = service.adjust(
        cabin_id,
        delta,
        note,
        person_id=request.str_field("person_id", "operator"),
        at=request.str_field("at"),
        event_id=request.str_field("event_id"),
    )
    record_event(server.runtime, "occupancy_adjust", cabin_id)
    return json_response(result.as_dict())


def occupancy_events(server: "Server", request: Request) -> Response:
    service = _service(server)
    cabin_id = request.str_field("cabin_id")
    events = service.events(cabin_id)
    return json_response(
        {
            "cabin_id": cabin_id,
            "events": [event.as_dict() for event in events],
            "count": len(events),
        }
    )


def occupancy_alarms(server: "Server", request: Request) -> Response:
    service = _service(server)
    return json_response({"alarms": [alarm.as_dict() for alarm in service.all_alarms()]})


def occupancy_alarm_ack(server: "Server", request: Request) -> Response:
    service = _service(server)
    key = request.str_field("key")
    if not key:
        raise RequestError(400, "alarm key is required")
    if not service.acknowledge_alarm(key):
        raise RequestError(404, f"alarm {key} not found")
    return json_response({"acknowledged": True, "key": key})


def occupancy_sweep(server: "Server", request: Request) -> Response:
    service = _service(server)
    return json_response(service.sweep(request.str_field("at")))
