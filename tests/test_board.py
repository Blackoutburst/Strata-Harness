from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from strata_board_harness.board import BoardClient, BoardError, MCPHTTP, TaskLease
from strata_board_harness.coordination import Identity, Journal, JournalConflict
from strata_board_harness.supervisor import Store


IDENTITY = Identity("fixture-owner", "fixture-worker", "fixture-device", "fixture-session")


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)
        self.addCleanup(lambda: self.store.close())
        self.journal = Journal(self.store, IDENTITY)
        self.now, self.alive = 1000000, True
        self.profile_version = 1
        self.http_status = 200
        self.wrong_identity = self.wrong_reply_id = self.conflict = False
        self.drop_after = None
        self.after_get = None
        self.requests, self.authorizations, self.mutations = [], [], []
        self.ticket = {"id": "selected-task", "version": 1, "status": "todo",
                       "assignee_owner_id": None, "assignee_agent_id": IDENTITY.agent_id,
                       "claimant": None, "lease_expires_at": None,
                       "blocked_by": [], "waiting_on_human": False}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.authorizations.append(self.headers.get("Authorization"))
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(request)
                if owner.http_status != 200:
                    self.send_response(owner.http_status)
                    if owner.http_status == 302:
                        self.send_header("Location", "/credential-trap")
                    self.end_headers()
                    return
                result = owner.respond(request)
                operation = request.get("params", {}).get("name")
                if owner.drop_after and operation == owner.drop_after:
                    owner.drop_after = None
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                data = {"jsonrpc": "2.0", "id": "wrong-id" if owner.wrong_reply_id else request["id"],
                        "result": result}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.environment = patch.dict(os.environ, {"HARNESS_TEST_BOARD_TOKEN": "fixture-only-private-token"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = BoardClient(MCPHTTP(f"http://127.0.0.1:{self.server.server_port}/mcp",
                                         token_env="HARNESS_TEST_BOARD_TOKEN"),
                                  IDENTITY, "fixture-binding", ["selected-task"])
        self.client.register()
        self.lease = self.make_lease()

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def make_lease(self):
        return TaskLease(self.journal, self.client, "selected-task",
                         execution_is_active=lambda: self.alive, now=lambda: self.now)

    def session(self):
        session = asdict(IDENTITY)
        session["id"] = session.pop("session_id")
        session["profile_version"] = self.profile_version
        if self.wrong_identity:
            session["agent_id"] = "manager-is-not-worker"
        return session

    def respond(self, request):
        if request["method"] == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake-board"}}
        tool, args = request["params"]["name"], request["params"]["arguments"]
        if args["session_id"] != IDENTITY.session_id:
            return {"isError": True, "content": [{"type": "text", "text": "wrong identity"}]}
        if tool == "register_agent":
            data = {"session": self.session(), "identity": {"id": IDENTITY.agent_id,
                    "owner_id": IDENTITY.owner_id, "device_id": IDENTITY.device_id,
                    "identity_key": args["identity_key"], "profile_version": self.profile_version}}
        elif tool == "read_inbox":
            data = {"session": self.session(), "cursor": "inbox-cursor", "messages": []}
        elif tool == "get_ticket":
            data = {"ticket": dict(self.ticket), "project": {"id": "fixture-project"}}
            if self.after_get:
                self.after_get()
        elif tool == "get_text":
            text = "Bonjour 🙂 — canonical message"
            digest = hashlib.sha256(text.encode()).hexdigest()
            offset = args["offset"]
            data = {"kind": "message", "id": args["identifier"], "field": args["field"],
                    "text": text[offset:], "offset": offset, "next_offset": None,
                    "sha256": digest, "chars": len(text), "utf8_bytes": len(text.encode())}
        else:
            self.mutations.append((tool, args))
            if self.conflict:
                self.ticket["version"] += 1
                self.conflict = False
            if args["expected_version"] != self.ticket["version"]:
                return {"isError": True, "content": [{"type": "text", "text": "version conflict"}]}
            self.ticket["version"] += 1
            if tool == "release_ticket":
                self.ticket.update(claimant=None, lease_expires_at=None, status=args["status"])
            else:
                self.ticket.update(claimant={"kind": "agent", **asdict(IDENTITY),
                                            "profile_version": self.profile_version},
                                   lease_expires_at=self.now + 1200, status="in_progress")
            data = dict(self.ticket)
        return {"structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}]}

    def test_exact_identity_claim_renew_release_uses_current_versions(self):
        self.assertEqual(self.lease.claim()["version"], 2)
        self.now += 300
        self.assertEqual(self.lease.renew("fresh build checkpoint")["version"], 3)
        released = self.lease.release("fixture review handoff", "review")
        self.assertEqual(released["version"], 4)
        self.assertEqual(released["assignee_agent_id"], IDENTITY.agent_id)
        self.assertIsNone(released["claimant"])
        self.assertEqual([args["expected_version"] for _, args in self.mutations], [1, 2, 3])
        self.assertTrue(all(args["session_id"] == IDENTITY.session_id for _, args in self.mutations))
        self.assertFalse(self.lease.state()["fenced"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM board_operations").fetchone()[0], 3)
        self.assertTrue(all(value == "Bearer fixture-only-private-token" for value in self.authorizations))

    def test_inbox_does_not_claim_and_profile_change_is_rejected(self):
        self.assertEqual(self.journal.poll_once(self.client.read_page), 0)
        self.assertEqual(self.mutations, [])
        self.profile_version += 1
        with self.assertRaises(BoardError):
            self.journal.poll_once(self.client.read_page)
        self.assertEqual(self.journal.cursor(), "inbox-cursor")

    def test_scope_assignment_and_other_claims_are_fenced_before_mutation(self):
        before = len(self.requests)
        with self.assertRaises(BoardError):
            self.client.ticket("unrelated-backlog")
        self.assertEqual(len(self.requests), before)
        for assignee in (None, "different-worker"):
            self.ticket["assignee_agent_id"] = assignee
            with self.assertRaises(BoardError):
                self.lease.claim()
        self.ticket["assignee_agent_id"] = IDENTITY.agent_id
        self.ticket["claimant"] = {"session_id": "another-live-session"}
        with self.assertRaises(JournalConflict):
            self.lease.claim()
        self.assertEqual(self.mutations, [])

    def test_dead_or_expired_execution_is_never_renewed_or_recovered(self):
        self.lease.claim()
        self.alive = False
        with self.assertRaises(JournalConflict):
            self.lease.renew()
        self.alive = True
        self.now += 1201
        with self.assertRaises(JournalConflict):
            self.lease.renew()
        with self.assertRaises(JournalConflict):
            self.lease.release("not authorized to clear an expired claim")
        self.assertEqual(len(self.mutations), 1)

    def test_worker_death_during_ticket_read_prevents_renewal(self):
        self.lease.claim()
        self.after_get = lambda: setattr(self, "alive", False)
        with self.assertRaises(JournalConflict):
            self.lease.renew()
        self.assertEqual(len(self.mutations), 1)

    def test_version_conflict_persists_fence_even_after_reread(self):
        self.lease.claim()
        self.conflict = True
        with self.assertRaises(BoardError):
            self.lease.renew()
        self.assertTrue(self.lease.state()["fenced"])
        self.assertEqual(self.lease.state()["pending"]["arguments"]["expected_version"], 2)
        self.lease.refresh()
        before = len(self.requests)
        with self.assertRaises(JournalConflict):
            self.lease.renew()
        self.assertEqual(len(self.requests), before)
        self.lease.resolve_uncertain({"inspection": "fixture execution and effects reconciled", "snapshot": "v3"})
        self.assertEqual(self.lease.renew()["version"], 4)

    def test_lost_claim_reply_survives_restart_without_duplicate_claim(self):
        self.drop_after = "claim_ticket"
        with self.assertRaises(BoardError) as caught:
            self.lease.claim()
        self.assertEqual(caught.exception.category, "unavailable")
        self.assertEqual(self.ticket["version"], 2)
        self.store.close()
        self.store = Store(self.directory)
        self.journal = Journal(self.store, IDENTITY)
        self.lease = self.make_lease()
        with self.assertRaises(JournalConflict):
            self.lease.claim()
        self.assertEqual(len(self.mutations), 1)
        self.lease.refresh()
        self.assertTrue(self.lease.state()["fenced"])
        with self.assertRaises(ValueError):
            self.lease.resolve_uncertain({})
        self.lease.resolve_uncertain({"inspection": "fixture process and effects verified; active owned claim observed"})
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.lease.renew()["version"], 3)

    def test_lost_release_reply_keeps_retry_key_and_never_clears_another_claim(self):
        self.lease.claim()
        self.drop_after = "release_ticket"
        with self.assertRaises(BoardError):
            self.lease.release("fixture shutdown", "todo")
        intent = self.lease.state()["pending"]
        self.assertEqual(intent["arguments"]["idempotency_key"], self.mutations[-1][1]["idempotency_key"])
        self.ticket["claimant"] = {"kind": "agent", **asdict(IDENTITY), "session_id": "other-session"}
        with self.assertRaises(JournalConflict):
            self.lease.resolve_uncertain({"inspection": "another execution now holds it"})
        self.assertTrue(self.lease.state()["fenced"])
        self.assertEqual(len(self.mutations), 2)

    def test_wrong_identity_failed_registration_and_wrong_rpc_id_are_rejected(self):
        self.wrong_identity = True
        with self.assertRaises(BoardError):
            self.client.register()
        with self.assertRaises(BoardError):
            self.client.ticket("selected-task")
        self.wrong_identity = False
        self.wrong_reply_id = True
        with self.assertRaises(BoardError) as caught:
            self.client.register()
        self.assertEqual(caught.exception.category, "invalid_response")
        self.assertEqual(self.mutations, [])

    def test_http_auth_redirect_and_missing_credentials_do_not_retry_or_leak(self):
        for status in (401, 302):
            self.http_status = status
            before = len(self.requests)
            with self.assertRaises(BoardError) as caught:
                self.client.register()
            self.assertEqual(caught.exception.category, "configuration")
            self.assertNotIn("fixture-only-private-token", str(caught.exception))
            self.assertEqual(len(self.requests), before + 1)
        with patch.dict(os.environ, {"HARNESS_TEST_BOARD_TOKEN": ""}):
            before = len(self.requests)
            with self.assertRaises(BoardError):
                self.client.register()
            self.assertEqual(len(self.requests), before)
        for endpoint in ("http://remote.example/mcp", "https://user:secret@example.com/mcp",
                         "https://example.com/mcp?token=secret"):
            with self.assertRaises(ValueError):
                MCPHTTP(endpoint)

    def test_no_completion_reassignment_or_identity_override_operation(self):
        self.lease.claim()
        with self.assertRaises(ValueError):
            self.lease.release("compilation is not acceptance", "done")
        with self.assertRaises(BoardError):
            self.client._mutate("update_ticket", "selected-task", {"assignee_agent_id": "other"})
        with self.assertRaises(BoardError):
            self.client._mutate("renew_ticket", "selected-task", {"session_id": "manager-session"})
        self.assertEqual(len(self.mutations), 1)

    def test_scoped_canonical_text_request_pins_hash_and_codepoint_offset(self):
        self.client.threads = frozenset(["selected-thread"])
        body = "Bonjour 🙂 — canonical message"
        digest = hashlib.sha256(body.encode()).hexdigest()
        message = {"id": "ping-1", "thread_id": "selected-thread", "text_fields": {"body": {
            "sha256": digest, "chars": len(body), "utf8_bytes": len(body.encode()),
            "get_text": {"kind": "message", "identifier": "ping-1", "field": "/body"}}}}
        chunk = self.client.read_body_chunk(message, 10)
        self.assertEqual(chunk["text"], body[10:])
        args = self.requests[-1]["params"]["arguments"]
        self.assertEqual(args["expected_sha256"], digest)
        self.assertEqual(args["offset"], 10)
        self.assertEqual(args["limit"], 2048)
        before = len(self.requests)
        message["thread_id"] = "unrelated-thread"
        with self.assertRaises(BoardError):
            self.client.read_body_chunk(message, 0)
        self.assertEqual(len(self.requests), before)


if __name__ == "__main__":
    unittest.main()
