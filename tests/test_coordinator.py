from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from strata_board_harness.board import BoardError, TaskLease
from strata_board_harness.coordination import Identity, Journal
from strata_board_harness.coordinator import Coordinator, Policy, WorkerState
from strata_board_harness.supervisor import Store


IDENTITY = Identity("fixture-owner", "fixture-worker", "fixture-device", "fixture-session")


def message(message_id, text, thread="selected-thread"):
    return {"id": message_id, "thread_id": thread, "body_preview": text[:2], "text_fields": {"body": {
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text), "utf8_bytes": len(text.encode()),
        "get_text": {"kind": "message", "identifier": message_id, "field": "/body"}}}}


class FakeBoard:
    def __init__(self, clock):
        self.identity, self.profile_version = IDENTITY, 1
        self.threads = frozenset(["selected-thread"])
        self.clock = clock
        self.inbox, self.bodies = [], {}
        self.poll_times, self.mutations, self.text_reads = [], [], []
        self.poll_error = self.text_error = False
        self.ticket_value = {"id": "selected-task", "version": 1, "status": "todo",
            "assignee_owner_id": None, "assignee_agent_id": IDENTITY.agent_id,
            "claimant": None, "lease_expires_at": None, "thread_id": "selected-thread"}

    def _selected(self, ticket_id):
        if ticket_id != "selected-task":
            raise BoardError("scope")

    def ticket(self, ticket_id):
        self._selected(ticket_id)
        return json.loads(json.dumps(self.ticket_value))

    def read_page(self, cursor):
        self.poll_times.append(self.clock())
        if self.poll_error:
            raise BoardError("unavailable")
        session = asdict(IDENTITY)
        session["id"] = session.pop("session_id")
        messages, self.inbox = self.inbox, []
        return {"session": session, "messages": messages, "cursor": "cursor-" + str(len(self.poll_times))}

    def _mutate(self, operation, ticket_id, arguments):
        self._selected(ticket_id)
        if arguments["expected_version"] != self.ticket_value["version"]:
            raise BoardError("tool_error")
        self.mutations.append((operation, self.clock(), arguments))
        self.ticket_value["version"] += 1
        if operation == "release_ticket":
            self.ticket_value.update(claimant=None, lease_expires_at=None, status=arguments["status"])
        else:
            self.ticket_value.update(claimant={"kind": "agent", **asdict(IDENTITY), "profile_version": 1},
                                     lease_expires_at=self.clock() + 1200, status="in_progress", claim_expired=False)
        return self.ticket(ticket_id)

    def read_body_chunk(self, envelope, offset):
        self.text_reads.append((envelope["id"], offset))
        body = self.bodies[envelope["id"]]
        end = min(offset + 3, len(body))
        return {"kind": "message", "id": envelope["id"], "field": "/body", "offset": offset,
                "text": body[offset:end], "next_offset": end if end < len(body) else None,
                "sha256": "0" * 64 if self.text_error else hashlib.sha256(body.encode()).hexdigest(),
                "chars": len(body), "utf8_bytes": len(body.encode())}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)
        self.addCleanup(lambda: self.store.close())
        self.now, self.active, self.progressing = 0, True, True
        self.quiet = self.clean = self.pause_error = False
        self.checkpoint = None
        self.pauses = []
        self.client = FakeBoard(lambda: self.now)
        self.policy = Policy(inbox_seconds=30, renew_seconds=100, offline_grace_seconds=120,
                             stale_progress_seconds=300, control_stale_seconds=300, chunks_per_tick=1)
        self.connect()
        self.lease.claim()

    def connect(self):
        self.journal = Journal(self.store, IDENTITY)
        self.lease = TaskLease(self.journal, self.client, "selected-task", lambda: self.active, lambda: self.now)
        self.coordinator = Coordinator(self.store, self.journal, self.client, self.worker,
                                       self.pause, self.lease, self.policy)

    def worker(self):
        return WorkerState(self.active, self.now if self.progressing else 0, "fixture-engine",
                           self.quiet, self.clean, self.checkpoint)

    def pause(self, request):
        self.pauses.append(request)
        if self.pause_error:
            raise RuntimeError("fixture lost pause reply")

    def tick(self, now, backend="model_available"):
        self.now = now
        return self.coordinator.tick(now, backend)

    def restart(self):
        self.store.close()
        self.store = Store(self.directory)
        self.connect()

    def test_thirty_minute_virtual_run_meets_board_cadence_without_model_turns(self):
        for now in range(0, 1801, 5):
            self.tick(now)
        renewals = [at for op, at, _ in self.client.mutations if op == "renew_ticket"]
        self.assertGreaterEqual(len(renewals), 18)
        self.assertTrue(all(b - a <= 100 for a, b in zip(renewals, renewals[1:])))
        self.assertTrue(all(b - a <= 30 for a, b in zip(self.client.poll_times, self.client.poll_times[1:])))
        self.assertEqual(self.pauses, [])
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(self.store.get("coordination_control")["mode"], "running")

    def test_offline_grace_keeps_inbox_alive_then_requires_verified_checkpoint_cleanup(self):
        self.tick(0)
        self.tick(10, "waiting_for_model")
        self.assertEqual(self.tick(110, "waiting_for_model")["mode"], "waiting_for_model")
        self.tick(130, "waiting_for_model")
        renewals = len([op for op, _, _ in self.client.mutations if op == "renew_ticket"])
        self.tick(131, "waiting_for_model")
        self.assertEqual(len(self.pauses), 1)
        self.assertEqual(self.pauses[0]["reason"], "model_offline_grace_elapsed")
        self.assertFalse(any(op == "release_ticket" for op, _, _ in self.client.mutations))
        self.active, self.quiet, self.clean = False, True, True
        self.checkpoint = {"head": "fixture-head", "effects": "fixture cleanup receipt"}
        self.assertEqual(self.tick(132, "waiting_for_model")["mode"], "released")
        self.assertEqual(self.tick(160)["mode"], "released")
        self.assertEqual(len([op for op, _, _ in self.client.mutations if op == "renew_ticket"]), renewals)
        self.assertEqual(self.journal.checkpoint("selected-task")["payload"], self.checkpoint)
        self.assertIsNone(self.client.ticket_value["claimant"])
        self.assertEqual(self.client.ticket_value["assignee_agent_id"], IDENTITY.agent_id)
        self.assertGreater(len(self.client.poll_times), 2)

    def test_idle_model_outage_is_normal_and_does_not_create_a_stop_or_claim(self):
        self.active = False
        coordinator = Coordinator(self.store, self.journal, self.client, self.worker, self.pause, policy=self.policy)
        self.coordinator = coordinator
        before = len(self.client.mutations)
        self.assertEqual(self.tick(0, "waiting_for_model")["mode"], "waiting_for_model")
        self.assertEqual(self.tick(2000, "waiting_for_model")["mode"], "waiting_for_model")
        self.assertEqual(self.tick(2001)["mode"], "idle")
        self.assertEqual(len(self.client.mutations), before)
        self.assertEqual(self.pauses, [])

    def test_dead_execution_is_not_renewed_or_released_without_effect_cleanup(self):
        self.active = False
        self.assertEqual(self.tick(0)["mode"], "stopped_requires_reconciliation")
        self.tick(100)
        self.assertEqual(len(self.client.mutations), 1)
        self.quiet = self.clean = True
        self.tick(101)
        self.assertEqual(len(self.client.mutations), 1)
        self.checkpoint = {"head": "fixture-head", "effects": "fixture cleanup"}
        self.assertEqual(self.tick(102)["mode"], "released")

    def test_stale_progress_and_uncertain_pause_survive_restart_without_duplicate_request(self):
        self.progressing = False
        self.tick(0)
        self.pause_error = True
        self.assertEqual(self.tick(300)["stop_reason"], "stale_progress")
        request = self.pauses[0]
        self.restart()
        self.tick(301)
        self.assertEqual(self.pauses, [request])
        self.assertEqual(self.store.get("coordination_control")["quiesce_request"]["id"], request["id"])
        self.assertEqual(self.store.get("coordination_control")["quiesce_error"], "pause_outcome_requires_reconciliation")
        self.assertEqual(len([op for op, _, _ in self.client.mutations if op == "renew_ticket"]), 1)

    def test_failed_pause_is_retried_with_the_same_intention_until_it_applies(self):
        self.progressing = False
        self.tick(0)
        self.pause_error = True
        self.tick(300)
        request = self.pauses[0]
        self.tick(330)
        self.assertEqual(self.pauses, [request])
        self.pause_error = False
        self.tick(361)
        self.assertEqual(self.pauses, [request, request])
        self.assertIsNone(self.store.get("coordination_control")["quiesce_error"])
        self.tick(500)
        self.assertEqual(len(self.pauses), 2)

    def test_progress_refreshed_during_a_slow_tick_is_not_a_clock_fault(self):
        self.tick(0)
        self.progressing = False
        self.worker = lambda: WorkerState(True, self.now + 40, "fixture-engine", False, False, None)
        self.coordinator.worker_state = self.worker
        self.assertIsNone(self.tick(10)["stop_reason"])
        self.worker = lambda: WorkerState(True, self.now + 4000, "fixture-engine", False, False, None)
        self.coordinator.worker_state = self.worker
        self.assertEqual(self.tick(20)["stop_reason"], "clock_reconciliation_required")

    def test_long_board_outage_stops_renewal_with_bounded_read_retry(self):
        self.tick(0)
        self.client.poll_error = True
        for now in range(5, 605, 5):
            self.tick(now)
        self.assertEqual(self.pauses[0]["reason"], "board_control_stale")
        self.assertEqual(len(self.pauses), 1)
        self.assertLess(len(self.client.poll_times), 12)
        self.assertTrue(all(at < 300 for op, at, _ in self.client.mutations if op == "renew_ticket"))

    def test_unicode_body_resumes_at_saved_codepoint_offset_and_skips_unrelated_thread(self):
        body = "🙂é\nManager: checkpoint the build."
        self.client.inbox = [message("ping-1", body), message("unrelated", "private other task", "other-thread")]
        self.client.bodies["ping-1"] = body
        self.tick(0)
        self.assertEqual(self.client.text_reads, [("ping-1", 0)])
        self.assertEqual(self.journal.pending()[0]["stage"], "received")
        self.restart()
        self.tick(1)
        self.assertEqual(self.client.text_reads[-1], ("ping-1", 3))
        for now in range(2, 30):
            self.tick(now)
        pending = {entry["id"]: entry for entry in self.journal.pending()}
        self.assertEqual(pending["ping-1"]["body"], body)
        self.assertEqual(pending["ping-1"]["stage"], "ready")
        self.assertEqual(pending["unrelated"]["stage"], "received")
        self.assertTrue(all(item[0] == "ping-1" for item in self.client.text_reads))
        self.assertEqual(self.client.text_reads.count(("ping-1", 0)), 1)

    def test_changed_source_hash_never_promotes_preview_or_discards_saved_prefix(self):
        body = "🙂abcdef"
        self.client.inbox = [message("ping-1", body)]
        self.client.bodies["ping-1"] = body
        self.tick(0)
        self.client.text_error = True
        self.assertEqual(self.tick(1)["text_error"], "canonical_source_requires_attention")
        self.assertEqual(self.journal.pending()[0]["stage"], "received")
        self.restart()
        self.client.text_error = False
        for now in range(2, 40):
            self.tick(now)
        self.assertEqual(self.journal.pending()[0]["body"], body)
        self.assertEqual(self.client.text_reads.count(("ping-1", 0)), 1)

    def test_bad_message_backoff_does_not_starve_a_following_valid_ping(self):
        bad = message("bad", "bad source")
        bad["text_fields"]["body"]["sha256"] = "invalid"
        self.client.inbox = [bad, message("good", "OK")]
        self.client.bodies["good"] = "OK"
        self.tick(0)
        self.tick(1)
        pending = {entry["id"]: entry for entry in self.journal.pending()}
        self.assertEqual(pending["bad"]["stage"], "received")
        self.assertEqual(pending["good"]["body"], "OK")
        self.assertEqual(self.client.text_reads, [("good", 0)])

    def test_long_body_and_short_ping_share_the_read_budget(self):
        self.client.inbox = [message("long", "A" * 100), message("ping", "OK")]
        self.client.bodies.update(long="A" * 100, ping="OK")
        self.tick(0)
        self.tick(1)
        pending = {entry["id"]: entry for entry in self.journal.pending()}
        self.assertEqual(pending["long"]["stage"], "received")
        self.assertEqual(pending["ping"]["stage"], "ready")

    def test_active_execution_without_selected_lease_requests_stop_without_claiming(self):
        self.coordinator = Coordinator(self.store, self.journal, self.client, self.worker,
                                       self.pause, policy=self.policy)
        before = len(self.client.mutations)
        self.assertEqual(self.tick(0)["stop_reason"], "execution_without_selected_lease")
        self.assertEqual(len(self.pauses), 1)
        self.assertEqual(len(self.client.mutations), before)

    def test_backend_configuration_failure_is_distinct_from_expected_model_shutdown(self):
        self.active = False
        self.coordinator = Coordinator(self.store, self.journal, self.client, self.worker,
                                       self.pause, policy=self.policy)
        state = self.tick(0, "configuration_error")
        self.assertEqual(state["mode"], "backend_configuration_error")
        self.assertEqual(state["backend"], "configuration_error")
        self.assertIsNone(state["stop_reason"])

    def test_offline_grace_persists_across_restart_and_clock_rewind_cannot_extend_work(self):
        self.tick(0, "waiting_for_model")
        self.restart()
        self.assertEqual(self.tick(120, "waiting_for_model")["stop_reason"], "model_offline_grace_elapsed")
        self.assertEqual(len(self.pauses), 1)
        self.assertEqual(len([op for op, _, _ in self.client.mutations if op == "renew_ticket"]), 1)
        # A separate fresh control observation must not silently extend deadlines.
        self.store.save({"coordination_control": {"mode": "running", "offline_since": None,
            "stop_reason": None, "quiesce_request": None, "poll_failures": 0, "last_tick_at": 200}})
        self.connect()
        self.assertEqual(self.tick(150)["stop_reason"], "clock_reconciliation_required")


if __name__ == "__main__":
    unittest.main()
