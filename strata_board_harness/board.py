"""Bounded stateless Board MCP client and a fenced, selected-task lease lifecycle.

The daemon composes this client with selected execution. No task discovery, worker
launch, expired-claim recovery or automatic retry is provided by this module.
"""

from dataclasses import asdict
from http.client import HTTPException
import json
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from uuid import uuid4

from .coordination import JournalConflict, encoded, required_text
from .supervisor import NoRedirect, utc_now


class BoardError(RuntimeError):
    def __init__(self, category, status=None):
        self.category, self.status = category, status
        # Do not echo HTTP bodies, tool prose or credentials into daemon logs.
        super().__init__("Board request failed: " + category)


class MCPHTTP:
    """The owned Board uses stateless_http=True and json_response=True."""

    def __init__(self, endpoint, token_env="BOARD_API_TOKEN", timeout=5):
        parsed = urlsplit(endpoint)
        local_http = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (not parsed.hostname or not (parsed.scheme == "https" or local_http)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Board endpoint requires HTTPS or loopback HTTP without embedded credentials")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 15:
            raise ValueError("Board timeout must be between zero and fifteen seconds")
        self.endpoint, self.token_env, self.timeout = endpoint, required_text(token_env), timeout
        self.opener = build_opener(NoRedirect())

    def rpc(self, method, params):
        token = os.environ.get(self.token_env)
        if not token or any(character in token for character in ("\r", "\n")):
            raise BoardError("configuration")
        request_id = uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        request = Request(self.endpoint, data=encoded(payload).encode("utf-8"), headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26",
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise BoardError("oversized_response")
                data = json.loads(body)
        except HTTPError as error:
            status = error.code
            error.close()
            category = "configuration" if status in (301, 302, 303, 307, 308, 401, 403) else "http_error"
            raise BoardError(category, status) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise BoardError("unavailable") from None
        except (ValueError, UnicodeError):
            raise BoardError("invalid_response") from None
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or data.get("id") != request_id:
            raise BoardError("invalid_response")
        if "error" in data:
            raise BoardError("rpc_error")
        result = data.get("result")
        if not isinstance(result, dict):
            raise BoardError("invalid_response")
        return result


class BoardClient:
    def __init__(self, transport, identity, identity_key, selected_ticket_ids, label="Strata harness worker",
                 selected_thread_ids=()):
        self.transport, self.identity = transport, identity
        self.identity_key, self.label = required_text(identity_key), required_text(label)
        self.selected = frozenset(required_text(item) for item in selected_ticket_ids)
        self.threads = frozenset(required_text(item) for item in selected_thread_ids)
        self.profile_version = None

    def register(self):
        self.profile_version = None
        initialization = self.transport.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "strata-board-harness", "version": "0.1.0"}})
        if initialization.get("protocolVersion") != "2025-03-26":
            raise BoardError("protocol_mismatch")
        data = self._tool("register_agent", {"label": self.label, "agent_kind": "other",
            "identity_key": self.identity_key, "name": self.label, "skills": ["android", "e2e"],
            "autonomous": True, "view": "full"}, registering=True)
        profile = data.get("identity", {})
        if (not self.identity.matches(data.get("session"))
                or profile.get("id") != self.identity.agent_id
                or profile.get("owner_id") != self.identity.owner_id
                or profile.get("device_id") != self.identity.device_id
                or profile.get("identity_key") != self.identity_key):
            raise BoardError("identity_mismatch")
        version = profile.get("profile_version")
        if type(version) is not int or version < 1 or data["session"].get("profile_version") != version:
            raise BoardError("profile_mismatch")
        self.profile_version = version

    def read_page(self, cursor):
        data = self._tool("read_inbox", {"label": self.label, "cursor": cursor,
            "limit": 50, "include_work": False, "view": "compact"})
        if (not self.identity.matches(data.get("session"))
                or data["session"].get("profile_version") != self.profile_version):
            raise BoardError("identity_mismatch")
        return data

    def ticket(self, ticket_id):
        self._selected(ticket_id)
        data = self._tool("get_ticket", {"ticket_id": ticket_id, "view": "full"})
        return data.get("ticket", {})

    def read_body_chunk(self, message, offset):
        if message.get("thread_id") not in self.threads:
            raise BoardError("scope")
        try:
            info = message["text_fields"]["body"]
            reference = info["get_text"]
            if reference != {"kind": "message", "identifier": message["id"], "field": "/body"}:
                raise BoardError("invalid_text_reference")
            digest = info["sha256"]
        except (KeyError, TypeError):
            raise BoardError("invalid_text_reference") from None
        return self._tool("get_text", {**reference, "offset": offset, "limit": 2048,
                                      "expected_sha256": digest})

    def post_selected_message(self, thread_id, body, idempotency_key, manager_agent_id):
        if thread_id not in self.threads:
            raise BoardError("scope")
        return self._tool("post_message", {"thread_id": thread_id, "body": required_text(body),
            "idempotency_key": required_text(idempotency_key),
            "mentions": ["agent:" + required_text(manager_agent_id)], "view": "full"})

    def _mutate(self, operation, ticket_id, arguments):
        self._selected(ticket_id)
        if operation not in ("claim_ticket", "renew_ticket", "release_ticket"):
            raise BoardError("scope")
        if any(key in arguments for key in ("session_id", "ticket_id")):
            raise BoardError("scope")
        return self._tool(operation, {"ticket_id": ticket_id, **arguments, "view": "full"})

    def _selected(self, ticket_id):
        if ticket_id not in self.selected:
            raise BoardError("scope")

    def _tool(self, name, arguments, registering=False):
        if not registering and self.profile_version is None:
            raise BoardError("not_registered")
        result = self.transport.rpc("tools/call", {"name": name,
            "arguments": {**arguments, "session_id": self.identity.session_id}})
        if result.get("isError"):
            raise BoardError("tool_error")
        data = result.get("structuredContent")
        if data is None:
            try:
                data = json.loads(result["content"][0]["text"])
            except (KeyError, IndexError, TypeError, ValueError):
                raise BoardError("invalid_response") from None
        if not isinstance(data, dict):
            raise BoardError("invalid_response")
        return data


def recoverable_own_claim(ticket, identity):
    claimant = ticket.get("claimant") or {}
    return bool(ticket.get("claim_expired") is True and claimant.get("kind") == "agent"
                and claimant.get("agent_id") == identity.agent_id and claimant.get("owner_id") == identity.owner_id
                and ticket.get("status") in ("todo", "in_progress"))


class TaskLease:
    """One coordinator thread serializes mutations via a durable in-flight slot.

    A conflict, crash or uncertain response fences further mutations. Reading the
    current ticket alone never clears that fence. No LLM or task scan is used.
    """
    def __init__(self, journal, client, ticket_id, execution_is_active, now=time.time):
        client._selected(ticket_id)
        if journal.identity != client.identity or not callable(execution_is_active):
            raise ValueError("Lease requires the journal's exact identity and an execution verifier")
        self.db, self.client, self.ticket_id = journal.db, client, ticket_id
        self.execution_is_active, self.now = execution_is_active, now
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS board_tasks (
                ticket_id TEXT PRIMARY KEY, snapshot TEXT, pending TEXT, fenced INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS board_operations (
                id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL, at TEXT NOT NULL,
                operation TEXT NOT NULL, evidence TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO board_tasks(ticket_id) VALUES (?)", (ticket_id,))

    def state(self):
        snapshot, pending, fenced = self.db.execute(
            "SELECT snapshot,pending,fenced FROM board_tasks WHERE ticket_id=?", (self.ticket_id,)).fetchone()
        return {"snapshot": json.loads(snapshot) if snapshot else None,
                "pending": json.loads(pending) if pending else None, "fenced": bool(fenced)}

    def refresh(self):
        ticket = self.client.ticket(self.ticket_id)
        self._validate(ticket)
        with self.db:
            self.db.execute("UPDATE board_tasks SET snapshot=? WHERE ticket_id=?",
                            (encoded(ticket), self.ticket_id))
        return ticket

    def claim(self, recovery_evidence=""):
        self._unfenced()
        ticket = self.refresh()
        if ticket.get("blocked_by") or ticket.get("waiting_on_human"):
            raise JournalConflict("Selected task is not unclaimed and ready")
        if ticket.get("claimant") or ticket.get("status") != "todo":
            # Only our own identity's EXPIRED claim, with the manager's explicit
            # reconciliation evidence, may be recovered; a live or foreign claim never.
            if not (recoverable_own_claim(ticket, self.client.identity) and recovery_evidence.strip()):
                raise JournalConflict("Selected task is not unclaimed and ready")
            return self._perform("claim_ticket", ticket, {"recovery_evidence": recovery_evidence})
        return self._perform("claim_ticket", ticket, {})

    def renew(self, progress=""):
        self._unfenced()
        if not self.execution_is_active():
            raise JournalConflict("No verified active execution; refusing renewal")
        ticket = self.refresh()
        self._owned_live(ticket)
        if not self.execution_is_active():
            raise JournalConflict("Execution stopped during ticket refresh; refusing renewal")
        return self._perform("renew_ticket", ticket, {"progress": progress})

    def release(self, reason, status="todo"):
        self._unfenced()
        required_text(reason)
        if status not in ("todo", "blocked", "review"):
            raise ValueError("Release cannot complete or reassign a task")
        ticket = self.refresh()
        self._owned_live(ticket)
        return self._perform("release_ticket", ticket, {"reason": reason, "status": status,
            "idempotency_key": uuid4().hex, "unassign_self": False})

    def resolve_uncertain(self, evidence):
        """Trusted coordinator recovery hook, not a model tool or automatic retry.

        Caller must inspect actual execution/worktree/external effects first and
        supply durable evidence. This only synchronizes local state; it never
        releases/reclaims a remote claim, renews it or executes work.
        """
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("Concrete reconciliation evidence is required")
        previous = self.state()
        ticket = self.refresh()
        if ticket.get("claimant"):
            self._owned_live(ticket)
        with self.db:
            self._event("reconciled", {"previous_pending": previous["pending"], "evidence": evidence,
                                       "current_version": ticket["version"]})
            self.db.execute("UPDATE board_tasks SET pending=NULL,fenced=0 WHERE ticket_id=?", (self.ticket_id,))

    def _unfenced(self):
        state = self.state()
        if state["pending"] or state["fenced"]:
            raise JournalConflict("Previous mutation needs reconciliation; no automatic retry")

    def _validate(self, ticket):
        if (not isinstance(ticket, dict) or ticket.get("id") != self.ticket_id
                or type(ticket.get("version")) is not int or ticket["version"] < 1):
            raise BoardError("invalid_ticket")
        identity = self.client.identity
        if ticket.get("assignee_owner_id") not in (None, identity.owner_id) or ticket.get("assignee_agent_id") != identity.agent_id:
            raise BoardError("assignment_mismatch")

    def _owned_live(self, ticket):
        identity = self.client.identity
        claimant = ticket.get("claimant") or {}
        if (claimant.get("kind") != "agent"
                or any(claimant.get(key) != value for key, value in asdict(identity).items())
                or claimant.get("profile_version") != self.client.profile_version):
            raise JournalConflict("Claim belongs to a different execution identity")
        expiry = ticket.get("lease_expires_at")
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= self.now():
            raise JournalConflict("Claim expired; process/effect recovery is required")
        if ticket.get("status") == "done":
            raise JournalConflict("Completed work cannot be renewed or released")

    def _perform(self, operation, ticket, arguments):
        arguments = {"expected_version": ticket["version"], **arguments}
        intent = {"operation": operation, "arguments": arguments, "at": utc_now()}
        with self.db:
            changed = self.db.execute("UPDATE board_tasks SET pending=? WHERE ticket_id=? AND pending IS NULL AND fenced=0",
                                      (encoded(intent), self.ticket_id)).rowcount
            if changed != 1:
                raise JournalConflict("Another task mutation is already pending")
        try:
            result = self.client._mutate(operation, self.ticket_id, arguments)
            self._validate(result)
            if result["version"] != ticket["version"] + 1:
                raise BoardError("unexpected_version")
            if operation == "release_ticket":
                if result.get("claimant") is not None or result.get("status") != arguments["status"]:
                    raise BoardError("unexpected_release")
            else:
                self._owned_live(result)
            with self.db:
                self._event(operation, {"request": intent, "result_version": result["version"]})
                self.db.execute("UPDATE board_tasks SET snapshot=?,pending=NULL,fenced=0 WHERE ticket_id=?",
                                (encoded(result), self.ticket_id))
            return result
        except Exception:
            # The remote mutation may have succeeded even if no usable reply arrived.
            with self.db:
                self.db.execute("UPDATE board_tasks SET fenced=1 WHERE ticket_id=?", (self.ticket_id,))
            raise

    def _event(self, operation, evidence):
        self.db.execute("INSERT INTO board_operations(ticket_id,at,operation,evidence) VALUES (?,?,?,?)",
                        (self.ticket_id, utc_now(), operation, encoded(evidence)))
