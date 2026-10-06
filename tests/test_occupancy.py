"""Behavioural tests for the occupancy ledger and its console surface."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest

from tunnelnet.access import AccessService
from tunnelnet.cabin import Cabin, Registry as CabinRegistry
from tunnelnet.console.server import Server
from tunnelnet.occupancy import OccupancyLedger
from tunnelnet.occupancy.ledger import SNAPSHOT_KIND
from tunnelnet.occupancy.order import canon
from tunnelnet.store import Store, list_approvals

T0 = "2026-10-06T08:00:00Z"
T1 = "2026-10-06T08:01:00Z"
T2 = "2026-10-06T08:02:00Z"
T3 = "2026-10-06T08:03:00Z"


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/data")
        self.ledger = OccupancyLedger(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def enter(self, cabin: str, person: str, at: str, event_id: str, **kw):
        return self.ledger.apply_event(
            cabin_id=cabin, person_id=person, direction="IN",
            event_time=at, event_id=event_id, **kw,
        )

    def exit(self, cabin: str, person: str, at: str, event_id: str, **kw):
        return self.ledger.apply_event(
            cabin_id=cabin, person_id=person, direction="OUT",
            event_time=at, event_id=event_id, **kw,
        )

    def test_entry_exit_counting_and_console_consistency(self) -> None:
        self.enter("c1", "alice", T0, "e1")
        self.enter("c1", "bob", T1, "e2")
        self.assertEqual(self.exit("c1", "alice", T2, "e3")["count_after"], 1)
        view = self.ledger.console("c1")
        self.assertEqual(view["in_count"], 1)
        self.assertTrue(view["consistent"])
        self.assertEqual([row["person_id"] for row in view["roster"]], ["bob"])

    def test_capacity_blocks_admission_and_denial_is_audited(self) -> None:
        self.ledger.configure("c1", capacity=2)
        first = self.ledger.admit(cabin_id="c1", person_id="p1", admission_id="a1")
        second = self.ledger.admit(cabin_id="c1", person_id="p2", admission_id="a2")
        third = self.ledger.admit(cabin_id="c1", person_id="p3", admission_id="a3")
        self.assertEqual(first.decision, "APPROVED")
        self.assertEqual(second.decision, "APPROVED")
        self.assertEqual(third.decision, "DENIED")
        self.assertEqual(third.reason, "capacity")
        self.assertEqual(third.count_at_decision, 2)
        # The denial quotes the same occupancy version the entries produced.
        self.assertEqual(third.occupancy_version, second.occupancy_version + 1)
        decisions = self.ledger.decisions("c1")
        self.assertEqual([item.decision for item in decisions], ["APPROVED", "APPROVED", "DENIED"])
        self.assertEqual(len(self.ledger.audit("c1")), 2)  # denials book no entry event
        self.assertTrue(self.ledger.console("c1")["consistent"])

    def test_admission_replay_returns_the_original_decision(self) -> None:
        self.ledger.configure("c1", capacity=1)
        first = self.ledger.admit(cabin_id="c1", person_id="p1", admission_id="a1")
        replay = self.ledger.admit(cabin_id="c1", person_id="p1", admission_id="a1")
        self.assertEqual(replay, first)
        self.assertEqual(len(self.ledger.audit("c1")), 1)

    def test_event_replay_is_idempotent(self) -> None:
        result = self.enter("c1", "alice", T0, "e1")
        self.assertEqual(result["status"], "applied")
        replay = self.enter("c1", "alice", T0, "e1")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["count_after"], result["count_after"])
        self.assertEqual(self.ledger.console("c1")["in_count"], 1)
        self.assertEqual(len(self.ledger.audit("c1")), 1)

    def test_out_of_order_pair_attributes_by_event_time(self) -> None:
        self.enter("c1", "alice", T1, "in-1")
        self.assertEqual(self.exit("c1", "alice", T3, "out-1")["count_after"], 0)
        # A re-entry that happened between the two arrives late over the network.
        late = self.enter("c1", "alice", T2, "in-2")
        self.assertEqual(late["status"], "recorded")
        self.assertEqual(late["anomaly"], "late_out_of_order")
        self.assertEqual(self.ledger.console("c1")["in_count"], 0)
        # The fact is still in the trail: at T2.5 alice was inside.
        self.assertEqual(self.ledger.replay("c1", at=canon("2026-10-06T08:02:30Z")), 1)
        self.assertTrue(self.ledger.console("c1")["consistent"])

    def test_double_entry_is_absorbed_by_the_state_machine(self) -> None:
        self.enter("c1", "alice", T0, "e1")
        again = self.enter("c1", "alice", T1, "e2")
        self.assertEqual(again["anomaly"], "double_entry")
        self.assertEqual(again["count_after"], 1)
        self.assertEqual(self.ledger.console("c1")["in_count"], 1)
        alerts = self.ledger.alerts("c1")
        self.assertEqual([alert.type for alert in alerts], ["anomaly"])

    def test_exit_without_presence_never_goes_negative(self) -> None:
        result = self.exit("c1", "ghost", T0, "e1")
        self.assertEqual(result["anomaly"], "exit_without_presence")
        self.assertEqual(result["count_after"], 0)
        self.assertEqual(self.ledger.console("c1")["in_count"], 0)

    def test_same_moment_pair_settles_exit_before_entry(self) -> None:
        for first, second in (("out", "in"), ("in", "out")):
            ledger = OccupancyLedger(Store.open(f"{self._tmp.name}/{first}-first"))
            ledger.apply_event(cabin_id="c1", person_id="alice", direction="IN",
                               event_time=T0, event_id=f"{first}-seed")
            pair = {"out": ("OUT", f"{first}-out"), "in": ("IN", f"{first}-in")}
            for name in (first, second):
                direction, event_id = pair[name]
                ledger.apply_event(cabin_id="c1", person_id="alice", direction=direction,
                                   event_time=T1, event_id=event_id)
            view = ledger.console("c1")
            self.assertEqual(view["in_count"], 1, f"{first} first")
            self.assertTrue(view["consistent"])

    def test_batch_partial_failure_and_idempotent_retry(self) -> None:
        batch = [
            {"cabin_id": "c1", "person_id": "alice", "direction": "IN",
             "event_time": T0, "event_id": "b1"},
            {"cabin_id": "c1", "person_id": "bob", "direction": "SIDEWAYS",
             "event_time": T0, "event_id": "b2"},
            {"cabin_id": "c1", "person_id": "carol", "direction": "IN",
             "event_time": T1, "event_id": "b3"},
        ]
        results = self.ledger.apply_batch(batch)
        self.assertEqual([item["status"] for item in results], ["applied", "error", "applied"])
        self.assertEqual(self.ledger.console("c1")["in_count"], 2)
        retry = self.ledger.apply_batch(batch)
        self.assertEqual([item["status"] for item in retry], ["duplicate", "error", "duplicate"])
        self.assertEqual(self.ledger.console("c1")["in_count"], 2)
        self.assertEqual(len(self.ledger.audit("c1")), 2)

    def test_concurrent_admissions_never_exceed_capacity(self) -> None:
        self.ledger.configure("c1", capacity=5)
        decisions = []
        errors = []

        def worker(index: int) -> None:
            try:
                decisions.append(
                    self.ledger.admit(cabin_id="c1", person_id=f"p{index}", admission_id=f"a{index}")
                )
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(decisions), 20)
        approved = [item for item in decisions if item.decision == "APPROVED"]
        self.assertEqual(len(approved), 5)
        view = self.ledger.console("c1")
        self.assertEqual(view["in_count"], 5)
        self.assertTrue(view["consistent"])

    def test_concurrent_events_with_duplicate_delivery(self) -> None:
        for index in range(8):
            self.enter("c1", f"p{index}", T0, f"in-{index}")

        def worker(index: int) -> None:
            self.exit("c1", f"p{index}", T1, f"out-{index}")
            self.enter("c1", f"p{index}", T2, f"rein-{index}")
            self.exit("c1", f"p{index}", T1, f"out-{index}")  # redelivery

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        view = self.ledger.console("c1")
        self.assertEqual(view["in_count"], 8)
        self.assertTrue(view["consistent"])
        self.assertEqual(len(self.ledger.audit("c1")), 8 + 16)

    def test_overstay_scan_alerts_once_per_stay(self) -> None:
        self.ledger.configure("c1", max_stay_minutes=30)
        self.enter("c1", "alice", T0, "e1")
        self.assertEqual(self.ledger.scan_overstay(at="2026-10-06T08:29:00Z"), 0)
        self.assertEqual(self.ledger.scan_overstay(at="2026-10-06T08:31:00Z"), 1)
        self.assertEqual(self.ledger.scan_overstay(at="2026-10-06T08:32:00Z"), 0)
        alerts = self.ledger.alerts("c1")
        self.assertEqual([alert.type for alert in alerts], ["overstay"])
        self.ledger.ack_alert(alerts[0].id)
        # Acknowledging does not re-arm the same stay.
        self.assertEqual(self.ledger.scan_overstay(at="2026-10-06T08:33:00Z"), 0)
        # A new stay is a new alert.
        self.exit("c1", "alice", T3, "e2")
        self.enter("c1", "alice", "2026-10-06T09:00:00Z", "e3")
        self.assertEqual(self.ledger.scan_overstay(at="2026-10-06T09:31:00Z"), 1)
        view = self.ledger.console("c1", at="2026-10-06T09:31:00Z")
        self.assertTrue(view["roster"][0]["overstayed"])

    def test_backfill_reveals_retroactive_over_capacity(self) -> None:
        self.ledger.configure("c1", capacity=1)
        self.enter("c1", "alice", "2026-10-01T08:01:00Z", "a-in")
        self.exit("c1", "alice", "2026-10-01T08:03:00Z", "a-out")
        # Bob's device was offline: he entered at 08:02 and only now uploads.
        result = self.enter("c1", "bob", "2026-10-01T08:02:00Z", "b-in", source="backfill")
        self.assertEqual(result["status"], "applied")
        view = self.ledger.console("c1")
        self.assertEqual(view["in_count"], 1)  # bob is still inside
        self.assertTrue(view["consistent"])
        types = [alert.type for alert in self.ledger.alerts("c1")]
        self.assertIn("retro_conflict", types)

    def test_device_sequence_gap_is_flagged(self) -> None:
        self.enter("c1", "alice", T0, "e1", device_id="gate-1", device_seq=1)
        self.enter("c1", "bob", T1, "e2", device_id="gate-1", device_seq=3)
        types = [alert.type for alert in self.ledger.alerts("c1")]
        self.assertIn("device_gap", types)

    def test_restore_replays_the_durable_log(self) -> None:
        self.ledger.configure("c1", capacity=3, max_stay_minutes=45)
        self.enter("c1", "alice", T0, "e1")
        self.enter("c1", "bob", T1, "e2")
        self.exit("c1", "alice", T2, "e3")
        self.assertEqual(self.ledger.scan_overstay(at="2026-10-06T09:00:00Z"), 1)
        self.ledger.admit(cabin_id="c1", person_id="carol", admission_id="a1")

        restored = OccupancyLedger(self.store)
        view = restored.console("c1")
        self.assertEqual(view["in_count"], 2)
        self.assertTrue(view["consistent"])
        self.assertEqual(len(restored.audit("c1")), 4)
        self.assertEqual(len(restored.decisions("c1")), 1)
        self.assertEqual(len(restored.alerts("c1")), 1)
        self.assertEqual(restored.limit("c1").capacity, 3)  # type: ignore[union-attr]
        # The restored ledger still rejects duplicate deliveries.
        result = restored.apply_event(cabin_id="c1", person_id="alice", direction="IN",
                                      event_time=T0, event_id="e1")
        self.assertEqual(result["status"], "duplicate")

    def test_snapshot_running_ahead_of_the_log_is_corruption(self) -> None:
        self.enter("c1", "alice", T0, "e1")
        self.store.save(SNAPSHOT_KIND, "c1", {"cabin_id": "c1", "in_count": 0, "version": 99})
        with self.assertRaises(ValueError):
            OccupancyLedger(self.store)


class AccessGateCase(unittest.TestCase):
    def test_approval_flow_is_gated_by_the_headcount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/data")
            cabins = CabinRegistry()
            cabins.upsert(Cabin(id="c1"))
            ledger = OccupancyLedger(store)
            ledger.configure("c1", capacity=1)
            access = AccessService(store, cabins, gate=ledger)

            approval = access.approve("c1", "alice")
            self.assertTrue(access.approved(approval.id))
            self.assertTrue(cabins.door_state("c1"))
            with self.assertRaises(ValueError):
                access.approve("c1", "bob")
            self.assertEqual(len(list_approvals(store)), 1)  # bob left no approval
            self.assertEqual(ledger.console("c1")["in_count"], 1)

            # Alice badges out; the slot frees up for bob.
            ledger.apply_event(cabin_id="c1", person_id="alice", direction="OUT",
                               event_time=T1, event_id="alice-out")
            access.approve("c1", "bob")
            self.assertEqual(ledger.console("c1")["in_count"], 1)

    def test_approval_entry_absorbs_the_turnstile_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/data")
            cabins = CabinRegistry()
            cabins.upsert(Cabin(id="c1"))
            ledger = OccupancyLedger(store)
            access = AccessService(store, cabins, gate=ledger)
            access.approve("c1", "alice")
            # The turnstile also reports alice's entry: same fact, new event id.
            result = ledger.apply_event(cabin_id="c1", person_id="alice", direction="IN",
                                        event_time=T1, event_id="turnstile-1")
            self.assertEqual(result["anomaly"], "double_entry")
            self.assertEqual(ledger.console("c1")["in_count"], 1)


class ConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/data")
        self.server = Server(self.store, web_dir="web")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None):
        response = self.server.dispatch(method, path, payload, query)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_gated_admission_over_http(self) -> None:
        status, limit = self.call(
            "POST", "/api/occupancy/configure",
            {"cabin_id": "cabin-1", "capacity": 1, "max_stay_minutes": 60},
        )
        self.assertEqual(status, 200)
        self.assertEqual(limit["capacity"], 1)

        status, first = self.call("POST", "/api/occupancy/admit", {"cabin_id": "cabin-1", "person_id": "alice"})
        self.assertEqual(status, 200)
        self.assertEqual(first["decision"], "APPROVED")
        self.assertEqual(first["in_count"], 1)

        status, second = self.call(
            "POST", "/api/occupancy/admit",
            {"cabin_id": "cabin-1", "person_id": "bob", "admission_id": "req-42"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["decision"], "DENIED")
        self.assertEqual(second["reason"], "capacity")
        self.assertEqual(second["count_at_decision"], 1)
        # Replaying the denied request returns the same decision, it does not retry.
        status, replay = self.call(
            "POST", "/api/occupancy/admit",
            {"cabin_id": "cabin-1", "person_id": "bob", "admission_id": "req-42"},
        )
        self.assertEqual(replay["decision"], "DENIED")

        status, view = self.call("GET", "/api/occupancy/console", query={"cabin_id": ["cabin-1"]})
        self.assertEqual(view["in_count"], 1)
        self.assertTrue(view["consistent"])
        self.assertEqual([row["person_id"] for row in view["roster"]], ["alice"])

        status, audit = self.call("GET", "/api/occupancy/audit", query={"cabin_id": ["cabin-1"]})
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["items"][0]["source"], "approval")
        self.assertEqual(audit["items"][0]["count_after"], view["in_count"])

        status, admissions = self.call(
            "GET", "/api/occupancy/admissions", query={"cabin_id": ["cabin-1"]}
        )
        self.assertEqual(admissions["count"], 2)

    def test_existing_approval_endpoint_is_gated(self) -> None:
        self.call("POST", "/api/occupancy/configure", {"cabin_id": "cabin-1", "capacity": 1})
        status, _ = self.call("POST", "/api/access/approve", {"cabin_id": "cabin-1", "requester": "alice"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/api/access/approve", {"cabin_id": "cabin-1", "requester": "bob"})
        self.assertEqual(status, 400)
        self.assertIn("capacity", body["error"])

    def test_event_ingest_batch_and_reconcile(self) -> None:
        status, result = self.call(
            "POST", "/api/occupancy/events",
            {"cabin_id": "cabin-1", "person_id": "alice", "direction": "IN",
             "event_time": T0, "event_id": "dev-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "applied")
        status, replay = self.call(
            "POST", "/api/occupancy/events",
            {"cabin_id": "cabin-1", "person_id": "alice", "direction": "IN",
             "event_time": T0, "event_id": "dev-1"},
        )
        self.assertEqual(replay["status"], "duplicate")

        status, batch = self.call(
            "POST", "/api/occupancy/events/batch",
            {"events": [
                {"cabin_id": "cabin-1", "person_id": "bob", "direction": "IN",
                 "event_time": T1, "event_id": "dev-2"},
                {"cabin_id": "cabin-1", "person_id": "carol", "direction": "IN",
                 "event_time": "not-a-time", "event_id": "dev-3"},
            ]},
        )
        self.assertEqual([item["status"] for item in batch["results"]], ["applied", "error"])

        status, view = self.call("POST", "/api/occupancy/reconcile", {"cabin_id": "cabin-1"})
        self.assertEqual(view["in_count"], 2)
        self.assertTrue(view["consistent"])

        status, overview = self.call("GET", "/api/occupancy")
        self.assertEqual([item["cabin_id"] for item in overview["items"]], ["cabin-1"])

    def test_unknown_cabin_and_missing_fields_are_rejected(self) -> None:
        status, body = self.call("POST", "/api/occupancy/admit", {"cabin_id": "ghost", "person_id": "x"})
        self.assertEqual(status, 404)
        status, body = self.call(
            "POST", "/api/occupancy/events",
            {"cabin_id": "cabin-1", "person_id": "x", "direction": "IN", "event_id": "e1"},
        )
        self.assertEqual(status, 400)
        self.assertIn("event_time", body["error"])

    def test_overstay_alert_surfaces_on_the_console(self) -> None:
        self.call("POST", "/api/occupancy/configure", {"cabin_id": "cabin-1", "max_stay_minutes": 1})
        self.call(
            "POST", "/api/occupancy/events",
            {"cabin_id": "cabin-1", "person_id": "alice", "direction": "IN",
             "event_time": "2026-10-05T00:00:00Z", "event_id": "old-1"},
        )
        status, scan = self.call("POST", "/api/occupancy/scan")
        self.assertEqual(scan["raised"], 1)
        status, alerts = self.call("GET", "/api/occupancy/alerts", query={"cabin_id": ["cabin-1"]})
        self.assertEqual(alerts["count"], 1)
        self.assertEqual(alerts["items"][0]["type"], "overstay")
        status, acked = self.call("POST", "/api/occupancy/alerts/ack", {"alert_id": alerts["items"][0]["id"]})
        self.assertEqual(acked["status"], "acked")
        status, alerts = self.call("GET", "/api/occupancy/alerts", query={"cabin_id": ["cabin-1"]})
        self.assertEqual(alerts["count"], 0)


if __name__ == "__main__":
    unittest.main()
