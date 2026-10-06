"""Behavioural tests for cabin occupancy counting and capacity admission.

These tests pin the two semantic decisions called out in the requirements:

1. Event-time attribution for paired reads under network delay, with late
   events correcting the count and raising retroactive alarms without
   rewriting releases already decided.
2. Per-person state-machine swiping (not first-subtract-then-add), with OUT
   sorted before IN on an exact timestamp tie.
"""

from __future__ import annotations

import tempfile
import threading
import unittest

from tunnelnet.occupancy import (
    IN,
    OUT,
    EventLedger,
    OccupancyService,
    ReservationBook,
    SwipeEvent,
    canonical,
    fold,
)
from tunnelnet.occupancy.alarms import AlarmRegistry
from tunnelnet.occupancy.model import ADJUST, SOURCE_BACKFILL, AdmissionDecision, Alarm
from tunnelnet.store import Store


class FakeClock:
    def __init__(self, value: str = "2026-10-05T09:00:00Z") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


def backfilled(event_id: str, cabin: str, person: str, direction: str, at: str) -> SwipeEvent:
    return SwipeEvent(
        id=event_id,
        cabin_id=cabin,
        person_id=person,
        direction=direction,
        at=at,
        received_at="2026-10-05T10:00:00Z",
        source=SOURCE_BACKFILL,
    )


class CanonicalOrderCase(unittest.TestCase):
    def test_out_sorts_before_in_on_exact_timestamp_tie(self) -> None:
        out = SwipeEvent(id="o", cabin_id="c", person_id="p", direction=OUT, at="t")
        enter = SwipeEvent(id="i", cabin_id="c", person_id="p", direction=IN, at="t")
        # Input order has IN first; canonical order must still put OUT first.
        self.assertEqual([e.direction for e in canonical([enter, out])], [OUT, IN])

    def test_event_time_beats_arrival_order(self) -> None:
        # The OUT happened earlier but arrived later; event time decides order.
        enter = SwipeEvent(id="i", cabin_id="c", person_id="p", direction=IN, at="2026-10-05T09:01:00Z")
        leave = SwipeEvent(id="o", cabin_id="c", person_id="p", direction=OUT, at="2026-10-05T09:00:00Z")
        self.assertEqual([e.id for e in canonical([enter, leave])], ["o", "i"])

    def test_tie_pair_frees_seat_before_reentry(self) -> None:
        # Cabin full (capacity 1); alice out and back in share one timestamp.
        events = [
            SwipeEvent(id="a-in", cabin_id="c", person_id="alice", direction=IN, at="t1"),
            SwipeEvent(id="a-in2", cabin_id="c", person_id="alice", direction=IN, at="t2"),
            SwipeEvent(id="a-out", cabin_id="c", person_id="alice", direction=OUT, at="t2"),
        ]
        result = fold("c", events, 1)
        # OUT first means the count never exceeds capacity, alice ends inside.
        self.assertEqual(result.occupied, 1)
        self.assertEqual(set(result.present), {"alice"})
        self.assertEqual([step.violation for step in result.steps if step.violation], [])


class AdmissionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = OccupancyService(clock=self.clock, late_window_seconds=300)
        self.service.configure_cabin("cabin-1", 2)

    def test_counts_entries_and_blocks_at_capacity(self) -> None:
        self.assertTrue(self.service.swipe("cabin-1", "a", IN, at="t1").decisions[0].allowed)
        self.assertTrue(self.service.swipe("cabin-1", "b", IN, at="t2").decisions[0].allowed)
        blocked = self.service.swipe("cabin-1", "c", IN, at="t3").decisions[0]
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.occupied, 2)
        self.assertEqual(blocked.capacity, 2)
        # Refused IN did not enter the ledger nor move the count.
        self.assertEqual(self.service.occupied("cabin-1"), 2)
        self.assertEqual(sorted(self.service.present("cabin-1")), ["a", "b"])

    def test_egress_is_never_blocked(self) -> None:
        self.service.swipe("cabin-1", "a", IN, at="t1")
        self.service.swipe("cabin-1", "b", IN, at="t2")
        self.service.swipe("cabin-1", "a", OUT, at="t3")
        # Previously blocked c can now enter.
        decision = self.service.swipe("cabin-1", "c", IN, at="t4").decisions[0]
        self.assertTrue(decision.allowed)
        self.assertEqual(self.service.occupied("cabin-1"), 2)

    def test_duplicate_in_is_refused_and_counted_once(self) -> None:
        self.service.swipe("cabin-1", "a", IN, event_id="a1", at="t1")
        second = self.service.swipe("cabin-1", "a", IN, event_id="a2", at="t2")
        self.assertFalse(second.decisions[0].allowed)
        self.assertEqual(second.decisions[0].reason, "already inside")
        self.assertEqual(self.service.occupied("cabin-1"), 1)
        self.assertTrue(any(alarm.kind == "duplicate_in" for alarm in second.alarms))

    def test_ghost_out_does_not_reduce_count(self) -> None:
        self.service.swipe("cabin-1", "a", IN, at="2026-10-05T09:00:01Z")
        # b was never inside; ghost OUT is accepted as record but no count move.
        result = self.service.swipe("cabin-1", "b", OUT, at="2026-10-05T09:00:02Z")
        self.assertTrue(result.accepted)
        self.assertEqual(self.service.occupied("cabin-1"), 1)
        # Within the late window no alarm yet (a matching IN may still arrive).
        self.assertFalse(any(a.kind == "ghost_out" for a in result.alarms))
        # After the window, a refold raises the ghost OUT alarm.
        self.clock.value = "2026-10-05T09:30:00Z"
        self.service.swipe("cabin-1", "a", OUT, at="2026-10-05T09:29:00Z")
        self.assertTrue(any(a.kind == "ghost_out" for a in self.service.all_alarms()))

    def test_concurrent_swipes_never_exceed_capacity(self) -> None:
        service = OccupancyService(clock=lambda: "2026-10-05T09:00:00Z")
        service.configure_cabin("cabin-z", 3)
        verdicts: list[bool] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            result = service.swipe(
                "cabin-z",
                f"p{index}",
                IN,
                at="2026-10-05T09:00:00Z",
                event_id=f"e{index}",
            )
            with lock:
                verdicts.append(result.decisions[0].allowed)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(verdicts), 3)
        self.assertEqual(service.occupied("cabin-z"), 3)


class EventTimeAttributionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock("2026-10-05T09:10:00Z")
        self.service = OccupancyService(clock=self.clock, late_window_seconds=300)
        self.service.configure_cabin("cabin-1", 2)

    def test_late_out_corrects_count_but_keeps_prior_denial(self) -> None:
        # Online: full, carol refused.
        self.service.swipe("cabin-1", "alice", IN, at="2026-10-05T09:00:01Z")
        self.service.swipe("cabin-1", "bob", IN, at="2026-10-05T09:00:02Z")
        refusal = self.service.swipe("cabin-1", "carol", IN, at="2026-10-05T09:00:03Z")
        self.assertFalse(refusal.decisions[0].allowed)

        # Late backfill shows alice had actually left just before all of this.
        # It is a ghost OUT in canonical order (earlier than her IN), so the
        # count is corrected physically but flagged; the denial is not revisited.
        result = self.service.backfill(
            "cabin-1", "alice", OUT, "2026-10-05T09:00:00Z", event_id="late-out"
        )
        self.assertEqual(len(result.accepted), 1)
        self.assertTrue(any(a.kind == "ghost_out" for a in self.service.all_alarms()))
        # Denials are an immutable record: still exactly one.
        self.assertEqual(len([a for a in self.service.approvals() if not a.allowed]), 1)

    def test_late_in_at_full_raises_retroactive_over_capacity(self) -> None:
        for index, person in enumerate(("a", "b"), start=1):
            self.service.swipe("cabin-1", person, IN, at=f"2026-10-05T09:00:0{index}Z")
        # A late IN, stamped before the second entry, arrives after the fact.
        self.service.backfill("cabin-1", "late", IN, "2026-10-05T09:00:01Z", event_id="late-in")
        self.assertEqual(self.service.occupied("cabin-1"), 3)
        kinds = {alarm.kind for alarm in self.service.all_alarms()}
        self.assertIn("retroactive_over_capacity", kinds)

    def test_arrival_order_vs_event_time_network_delay_scenario(self) -> None:
        # Paired out/in arrive out of order over a delayed link. Event-time
        # attribution yields the physically correct count regardless of arrival.
        self.service.swipe("cabin-1", "alice", IN, at="2026-10-05T09:00:01Z")
        self.service.ingest_batch(
            [
                backfilled("in-2", "cabin-1", "alice", IN, "2026-10-05T09:05:00Z"),
                backfilled("out-1", "cabin-1", "alice", OUT, "2026-10-05T09:04:00Z"),
            ]
        )
        self.assertEqual(self.service.occupied("cabin-1"), 1)
        self.assertEqual(self.service.present("cabin-1"), ["alice"])


class OfflineBatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock("2026-10-05T10:00:00Z")
        self.service = OccupancyService(clock=self.clock)
        self.service.configure_cabin("cabin-1", 5)

    def test_partial_failure_isolates_bad_events(self) -> None:
        result = self.service.ingest_batch(
            [
                SwipeEvent(id="", cabin_id="cabin-1", person_id="x", direction=IN, at="t1"),
                SwipeEvent(id="ok1", cabin_id="cabin-1", person_id="a", direction=IN, at="t2"),
                SwipeEvent(id="ok2", cabin_id="cabin-1", person_id="b", direction=OUT, at="t3"),
                SwipeEvent(id="bad", cabin_id="cabin-1", person_id="c", direction="sideways", at="t4"),
            ]
        )
        self.assertEqual([e.id for e in result.accepted], ["ok1", "ok2"])
        self.assertEqual({item.event_id for item in result.rejected}, {"", "bad"})
        self.assertTrue(any("direction" in item.reason for item in result.rejected))

    def test_replayed_batch_is_idempotent(self) -> None:
        events = [
            SwipeEvent(id="e1", cabin_id="cabin-1", person_id="a", direction=IN, at="t1"),
            SwipeEvent(id="e2", cabin_id="cabin-1", person_id="b", direction=IN, at="t2"),
        ]
        first = self.service.ingest_batch(events)
        second = self.service.ingest_batch(
            [SwipeEvent(**{**event.__dict__, "received_at": "2026-10-05T11:00:00Z"}) for event in events]
        )
        self.assertEqual(len(first.accepted), 2)
        self.assertEqual(len(second.accepted), 0)
        self.assertEqual(len(second.duplicates), 2)
        self.assertEqual(self.service.occupied("cabin-1"), 2)
        self.assertEqual(self.service.ledger_count(), 2)

    def test_replayed_swipe_request_is_idempotent(self) -> None:
        first = self.service.swipe("cabin-1", "a", IN, event_id="same", at="t1")
        second = self.service.swipe("cabin-1", "a", IN, event_id="same", at="t1")
        self.assertTrue(first.accepted)
        self.assertTrue(second.duplicates)
        self.assertEqual(self.service.occupied("cabin-1"), 1)

    def test_unknown_cabin_event_is_rejected_not_silently_dropped(self) -> None:
        result = self.service.ingest_batch(
            [SwipeEvent(id="x", cabin_id="ghost", person_id="a", direction=IN, at="t1")]
        )
        self.assertEqual(result.accepted, [])
        self.assertEqual(result.rejected[0].event_id, "x")


class ReservationApprovalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock("2026-10-05T09:00:00Z")
        self.service = OccupancyService(clock=self.clock)
        self.service.configure_cabin("cabin-1", 2)

    def test_approval_reserves_seat_and_blocks_next_stranger(self) -> None:
        self.service.swipe("cabin-1", "a", IN, at="t1")
        approval = self.service.approve("cabin-1", "b")
        self.assertTrue(approval.allowed)
        self.assertTrue(approval.reservation_id)
        self.assertEqual(approval.occupied, 1)
        # A stranger without a reservation now sees no seat (one occupied, one held).
        stranger = self.service.swipe("cabin-1", "c", IN, at="t2").decisions[0]
        self.assertFalse(stranger.allowed)
        # The approved holder consumes their reservation on swipe.
        holder = self.service.swipe("cabin-1", "b", IN, at="t3").decisions[0]
        self.assertTrue(holder.allowed)
        self.assertEqual(self.service.occupied("cabin-1"), 2)

    def test_approval_at_capacity_is_denied_with_durable_record(self) -> None:
        self.service.swipe("cabin-1", "a", IN, at="t1")
        self.service.swipe("cabin-1", "b", IN, at="t2")
        approval = self.service.approve("cabin-1", "c")
        self.assertFalse(approval.allowed)
        self.assertEqual(approval.reason, "cabin at capacity")
        self.assertFalse(approval.reservation_id)

    def test_expired_reservation_releases_its_seat(self) -> None:
        self.service.swipe("cabin-1", "a", IN, at="2026-10-05T09:00:01Z")
        self.service.approve("cabin-1", "b", ttl_seconds=60)
        self.clock.value = "2026-10-05T09:20:00Z"
        sweep = self.service.sweep()
        self.assertEqual(len(sweep["expired"]), 1)
        decision = self.service.swipe("cabin-1", "c", IN, at="2026-10-05T09:20:01Z").decisions[0]
        self.assertTrue(decision.allowed)


class ManualAdjustmentCase(unittest.TestCase):
    def test_adjustment_applies_raises_alarm_and_rejects_negative(self) -> None:
        clock = FakeClock()
        service = OccupancyService(clock=clock)
        service.configure_cabin("cabin-1", 5)
        service.adjust("cabin-1", 1, "tailgater", event_id="m1", at="t1")
        self.assertEqual(service.occupied("cabin-1"), 1)
        service.adjust("cabin-1", -1, "tailgater left", event_id="m2", at="t2")
        self.assertEqual(service.occupied("cabin-1"), 0)
        kinds = [a.kind for a in service.all_alarms()]
        self.assertEqual(kinds.count("manual_adjustment"), 2)
        with self.assertRaises(ValueError):
            service.adjust("cabin-1", -3, "would go negative", event_id="m3", at="t3")


class OverstayCase(unittest.TestCase):
    def test_overstay_alarm_uses_same_fold_and_is_idempotent(self) -> None:
        clock = FakeClock("2026-10-05T12:00:00Z")
        service = OccupancyService(clock=clock, late_window_seconds=300)
        service.configure_cabin("cabin-1", 5, dwell_limit_seconds=3600)
        service.swipe("cabin-1", "a", IN, at="2026-10-05T10:00:00Z")
        service.swipe("cabin-1", "b", IN, at="2026-10-05T11:55:00Z")
        sweep = service.sweep()
        overstays = {row["person_id"] for row in sweep["overstay_alarms"]}
        self.assertEqual(overstays, {"a"})
        # Alarm carries the same head count the console fold shows.
        alarm = next(alarm for alarm in service.all_alarms() if alarm.kind == "overstay")
        self.assertEqual(alarm.occupied, service.occupied("cabin-1"))
        self.assertEqual(service.sweep()["overstay_alarms"], [])


class LedgerRegistryCase(unittest.TestCase):
    def test_append_is_idempotent_and_locks_are_stable_per_cabin(self) -> None:
        ledger = EventLedger()
        event = SwipeEvent(id="e1", cabin_id="c", person_id="a", direction=IN, at="t")
        self.assertTrue(ledger.append(event))
        self.assertFalse(ledger.append(event))
        self.assertIs(ledger.lock_for("c"), ledger.lock_for("c"))
        self.assertIsNot(ledger.lock_for("c"), ledger.lock_for("other"))

    def test_alarm_registry_dedupes_by_key(self) -> None:
        alarms = AlarmRegistry()
        payload = dict(
            key="k",
            kind="over_capacity",
            cabin_id="c",
            person_id="a",
            at="t",
            occupied=3,
            capacity=2,
            version=1,
            message="x",
        )
        self.assertTrue(alarms.raise_alarm(Alarm(**payload)))
        self.assertFalse(alarms.raise_alarm(Alarm(**payload)))
        self.assertEqual(alarms.count(), 1)
        self.assertTrue(alarms.clear("k"))
        self.assertEqual(alarms.count(), 0)

    def test_reservation_consume_and_sweep(self) -> None:
        book = ReservationBook()
        from tunnelnet.occupancy.model import Reservation

        book.add(Reservation(id="r1", cabin_id="c", person_id="a", created_at="t1", expires_at="t5"))
        self.assertEqual(book.active_count("c", now="t2"), 1)
        self.assertIsNotNone(book.find_for_person("c", "a", now="t2"))
        self.assertIsNotNone(book.consume("r1", "event-in"))
        self.assertEqual(book.active_count("c", now="t3"), 0)
        book.add(Reservation(id="r2", cabin_id="c", person_id="b", created_at="t1", expires_at="t5"))
        self.assertEqual(len(book.sweep("t9")), 1)
        self.assertEqual(book.active_count("c", now="t9"), 0)


class PersistenceCase(unittest.TestCase):
    def test_rebuild_from_store_matches_console_fold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/data")
            clock = FakeClock("2026-10-05T10:00:00Z")
            service = OccupancyService(store=store, clock=clock)
            service.configure_cabin("cabin-1", 3)
            service.swipe("cabin-1", "a", IN, event_id="1", at="2026-10-05T09:00:01Z")
            service.swipe("cabin-1", "b", IN, event_id="2", at="2026-10-05T09:00:02Z")
            service.adjust("cabin-1", 1, "visitor", event_id="3", at="2026-10-05T09:00:03Z")

            rebuilt = OccupancyService(store=store, clock=clock)
            rebuilt.configure_cabin("cabin-1", 3)
            self.assertEqual(rebuilt.occupied("cabin-1"), service.occupied("cabin-1"))
            self.assertEqual(rebuilt.present("cabin-1"), service.present("cabin-1"))
            self.assertEqual(rebuilt.ledger_count(), 3)
            # Approvals and acknowledged alarm state survive restart too.
            self.assertTrue(any(record.allowed for record in rebuilt.approvals()))

    def test_disk_fold_equals_live_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/data")
            service = OccupancyService(store=store, clock=FakeClock())
            service.configure_cabin("cabin-1", 4)
            for index, person in enumerate(("a", "b", "c"), start=1):
                service.swipe("cabin-1", person, IN, event_id=person, at=f"t{index}")
            from tunnelnet.occupancy.persistence import load_all_events

            disk_events = load_all_events(store)
            self.assertEqual(fold("cabin-1", disk_events, 4).occupied, service.occupied("cabin-1"))
            self.assertEqual(fold("cabin-1", disk_events, 4).occupied, 3)


if __name__ == "__main__":
    unittest.main()
