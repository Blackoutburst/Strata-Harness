from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from strata_board_harness.coordination import Identity, Journal, JournalConflict
from strata_board_harness.supervisor import Store


IDENTITY = Identity("fixture-owner", "fixture-worker", "fixture-device", "fixture-session")
BODY = "Please checkpoint the failing build, then report its exact error."
DIGEST = hashlib.sha256(BODY.encode()).hexdigest()
MESSAGE = {"id": "ping-1", "thread_id": "selected-task-thread", "body_preview": "Please checkpoint…"}


def page(cursor, messages=None):
    session = asdict(IDENTITY)
    session["id"] = session.pop("session_id")
    return {"session": session, "cursor": cursor, "messages": messages or []}


class FakeBoard:
    """Compact cursor presentation acknowledges the preceding fixture page."""
    def __init__(self):
        self.calls = []

    def read_page(self, cursor):
        self.calls.append(cursor)
        if cursor is None:
            return page("cursor-1", [MESSAGE])
        return page("cursor-2")


class FakeEngine:
    def __init__(self):
        self.requests = {}

    def submit(self, entry):
        request_id = entry["request_id"]
        if request_id in self.requests:
            raise AssertionError("The caller attempted duplicate submission")
        self.requests[request_id] = {"request_id": request_id, "disposition": "queued"}


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)
        self.addCleanup(lambda: self.store.close())
        self.journal = Journal(self.store, IDENTITY)

    def restart(self):
        self.store.close()
        self.store = Store(self.directory)
        self.journal = Journal(self.store, IDENTITY)

    def ready(self):
        self.journal.persist_page(None, page("cursor-1", [MESSAGE]))
        self.journal.save_body("ping-1", BODY, DIGEST)

    def child(self, action):
        code = """
import json, os, sys
from pathlib import Path
from strata_board_harness.coordination import Identity, Journal
from strata_board_harness.supervisor import Store
store=Store(Path(sys.argv[1]))
journal=Journal(store,Identity(**json.loads(sys.argv[2])))
""" + action
        return subprocess.run([sys.executable, "-c", code, str(self.directory), json.dumps(asdict(IDENTITY)),
                               json.dumps(page("cursor-1", [MESSAGE]))],
                              capture_output=True, text=True, timeout=10)

    def test_page_and_checkpoint_survive_restart_before_next_cursor_ack(self):
        board = FakeBoard()
        self.journal.save_checkpoint("selected-task", "engine-1", {"head": "fixture-head", "dirty": True})
        self.assertEqual(self.journal.poll_once(board.read_page), 1)
        self.assertEqual(board.calls, [None])
        self.restart()
        self.assertEqual(self.journal.pending()[0]["id"], "ping-1")
        self.assertTrue(self.journal.checkpoint("selected-task")["payload"]["dirty"])
        self.assertEqual(self.journal.poll_once(board.read_page), 0)
        self.assertEqual(board.calls, [None, "cursor-1"])
        self.assertEqual(self.journal.cursor(), "cursor-2")
        self.assertEqual(len(self.journal.pending()), 1)

    def test_empty_polls_do_not_submit_to_engine_or_lose_pending_guidance(self):
        engine = FakeEngine()
        self.ready()
        for index in range(50):
            self.journal.poll_once(lambda _: page("empty-" + str(index)))
        self.assertEqual(engine.requests, {})
        self.assertEqual(self.journal.pending()[0]["stage"], "ready")

    def test_duplicate_page_and_message_are_deduplicated(self):
        self.journal.persist_page(None, page("cursor-1", [MESSAGE, MESSAGE]))
        self.journal.save_body("ping-1", BODY, DIGEST)
        self.journal.persist_page("cursor-1", page("cursor-2", [MESSAGE]))
        self.assertEqual(len(self.journal.pending()), 1)
        self.assertEqual(self.journal.pending()[0]["stage"], "ready")

    def test_bad_page_rolls_back_inserted_messages_and_cursor(self):
        bad = {"id": ""}
        with self.assertRaises(ValueError):
            self.journal.persist_page(None, page("cursor-1", [MESSAGE, bad]))
        self.assertEqual(self.journal.pending(), [])
        self.assertIsNone(self.journal.cursor())
        self.journal.persist_page(None, page("cursor-1", [MESSAGE]))
        altered = dict(MESSAGE, body_preview="Changed remote content")
        with self.assertRaises(JournalConflict):
            self.journal.persist_page("cursor-1", page("cursor-2", [altered]))
        self.assertEqual(self.journal.cursor(), "cursor-1")

    def test_stale_cursor_or_wrong_identity_cannot_advance_reader(self):
        self.ready()
        with self.assertRaises(JournalConflict):
            self.journal.persist_page(None, page("stale", [dict(MESSAGE, id="ping-2")]))
        self.assertEqual(len(self.journal.pending()), 1)
        wrong = page("wrong")
        wrong["session"]["agent_id"] = "manager-is-not-worker"
        with self.assertRaises(JournalConflict):
            self.journal.persist_page("cursor-1", wrong)
        with self.assertRaises(JournalConflict):
            Journal(self.store, replace(IDENTITY, session_id="another-session"))
        self.assertEqual(self.journal.cursor(), "cursor-1")

    def test_delivery_requires_canonical_hash_and_never_promotes_preview(self):
        self.journal.persist_page(None, page("cursor-1", [MESSAGE]))
        with self.assertRaises(JournalConflict):
            self.journal.prepare_delivery("ping-1", "engine-1", "request-1")
        with self.assertRaises(JournalConflict):
            self.journal.save_body("ping-1", "truncated or modified", DIGEST)
        self.journal.save_body("ping-1", BODY, DIGEST)
        entry = self.journal.prepare_delivery("ping-1", "engine-1", "request-1")
        self.assertEqual(entry["body"], BODY)
        self.assertEqual(entry["stage"], "prepared")

    def test_uncertain_submission_is_retained_for_reconciliation_not_resent(self):
        self.ready()
        engine = FakeEngine()
        entry = self.journal.prepare_delivery("ping-1", "engine-1", "request-1")
        engine.submit(entry)
        # External acceptance happened, but the worker died before saving a receipt.
        self.restart()
        entry = self.journal.pending()[0]
        self.assertEqual(entry["stage"], "prepared")
        self.assertEqual(entry["request_id"], "request-1")
        with self.assertRaises(JournalConflict):
            self.journal.prepare_delivery("ping-1", "engine-1", "new-request-id")
        self.journal.record_receipt("ping-1", "submitted", engine.requests[entry["request_id"]])
        self.assertEqual(len(engine.requests), 1)
        self.assertEqual(self.journal.pending()[0]["stage"], "submitted")

    def test_receipts_are_ordered_idempotent_and_application_is_distinct(self):
        self.ready()
        self.journal.prepare_delivery("ping-1", "engine-1", "request-1")
        with self.assertRaises(JournalConflict):
            self.journal.record_receipt("ping-1", "applied", {"claim": "queued is not applied"})
        submitted = {"request_id": "request-1", "event": "accepted"}
        self.journal.record_receipt("ping-1", "submitted", submitted)
        self.journal.record_receipt("ping-1", "acknowledged", {"event_id": "seen-1"})
        self.assertEqual(len(self.journal.pending()), 1)
        self.restart()
        self.journal.record_receipt("ping-1", "submitted", submitted)
        self.journal.record_receipt("ping-1", "applied", {"effect_receipt": "build-checkpoint-1"})
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(len(self.journal.receipts("ping-1")), 3)
        with self.assertRaises(JournalConflict):
            self.journal.record_receipt("ping-1", "applied", {"effect_receipt": "changed"})

    def test_actual_process_crash_during_page_transaction_replays_without_loss(self):
        result = self.child("""
store.connection.create_function('crash_now',0,lambda: os._exit(77))
store.connection.execute('CREATE TEMP TRIGGER crash_insert AFTER INSERT ON coord_messages BEGIN SELECT crash_now(); END')
journal.persist_page(None,json.loads(sys.argv[3]))
""")
        self.assertEqual(result.returncode, 77, result.stderr)
        self.restart()
        self.assertIsNone(self.journal.cursor())
        self.assertEqual(self.journal.pending(), [])
        board = FakeBoard()
        self.assertEqual(self.journal.poll_once(board.read_page), 1)
        self.assertEqual(board.calls, [None])

    def test_actual_process_crash_after_commit_retains_page_and_checkpoint(self):
        result = self.child("""
journal.save_checkpoint('selected-task','engine-1',{'head':'fixture-head','dirty':True})
journal.persist_page(None,json.loads(sys.argv[3]))
os._exit(78)
""")
        self.assertEqual(result.returncode, 78, result.stderr)
        self.restart()
        self.assertEqual(self.journal.cursor(), "cursor-1")
        self.assertEqual(len(self.journal.pending()), 1)
        self.assertTrue(self.journal.checkpoint("selected-task")["payload"]["dirty"])
        with self.assertRaises(JournalConflict):
            self.journal.save_checkpoint("selected-task", "different-engine", {"head": "new"})


if __name__ == "__main__":
    unittest.main()
