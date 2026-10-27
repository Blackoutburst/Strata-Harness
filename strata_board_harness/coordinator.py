"""Deterministic Board timing and canonical inbox retrieval; no inference calls.

Standalone component, not yet connected to a real engine or daemon configuration.
Worker state and quiesce callbacks must come from a verified engine adapter.
"""

from dataclasses import dataclass
import hashlib
import math
from uuid import uuid4

from .board import BoardError
from .coordination import JournalConflict, required_text

FUTURE_PROGRESS_TOLERANCE_SECONDS = 300
QUIESCE_RETRY_SECONDS = 60


@dataclass(frozen=True)
class Policy:
    inbox_seconds: float = 30
    renew_seconds: float = 300
    offline_grace_seconds: float = 300
    stale_progress_seconds: float = 900
    control_stale_seconds: float = 600
    chunks_per_tick: int = 2

    def __post_init__(self):
        for value in (self.inbox_seconds, self.renew_seconds, self.offline_grace_seconds,
                      self.stale_progress_seconds, self.control_stale_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Coordination deadlines must be finite positive numbers")
        if self.inbox_seconds > 600 or self.renew_seconds > 600 or self.control_stale_seconds > 600:
            raise ValueError("Board coordination deadlines must not exceed ten minutes")
        if type(self.chunks_per_tick) is not int or not 1 <= self.chunks_per_tick <= 8:
            raise ValueError("Chunk work per tick must be bounded")


@dataclass(frozen=True)
class WorkerState:
    active: bool
    progress_at: float
    engine_session: str = ""
    quiescent: bool = False
    cleanup_verified: bool = False
    checkpoint: dict | None = None
    release_status: str = "todo"

    def __post_init__(self):
        if any(type(value) is not bool for value in (self.active, self.quiescent, self.cleanup_verified)):
            raise ValueError("Worker observations require explicit booleans")
        if type(self.progress_at) not in (int, float) or not math.isfinite(self.progress_at):
            raise ValueError("Worker progress requires a finite observation time")
        if self.release_status not in ("todo", "blocked", "review"):
            raise ValueError("Worker cannot accept/complete a ticket")
        if self.active:
            required_text(self.engine_session)


class CanonicalInbox:
    MAX_BYTES = 1024 * 1024

    def __init__(self, journal, client):
        self.journal, self.client, self.db = journal, client, journal.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS inbox_text_progress (
            message_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, partial TEXT NOT NULL)""")
        self.db.commit()

    def step(self, entry):
        message = entry["envelope"]
        if entry["stage"] != "received" or message.get("thread_id") not in self.client.threads:
            return False
        try:
            info = message["text_fields"]["body"]
            digest, chars, byte_count = info["sha256"], info["chars"], info["utf8_bytes"]
        except (KeyError, TypeError):
            raise BoardError("invalid_text_reference") from None
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or type(chars) is not int or type(byte_count) is not int
                or not 0 <= chars <= self.MAX_BYTES or not 0 <= byte_count <= self.MAX_BYTES):
            raise BoardError("invalid_text_reference")
        saved = self.db.execute("SELECT sha256,partial FROM inbox_text_progress WHERE message_id=?",
                                (entry["id"],)).fetchone()
        if saved and saved[0] != digest:
            raise JournalConflict("Canonical source changed during retrieval")
        partial = saved[1] if saved else ""
        offset = len(partial)
        chunk = self.client.read_body_chunk(message, offset)
        text, next_offset = chunk.get("text"), chunk.get("next_offset")
        if (not isinstance(text, str) or chunk.get("offset") != offset
                or chunk.get("kind") != "message" or chunk.get("id") != entry["id"]
                or chunk.get("field") != "/body" or chunk.get("sha256") != digest
                or chunk.get("chars") != chars or chunk.get("utf8_bytes") != byte_count):
            raise BoardError("invalid_text_chunk")
        combined = partial + text
        if len(combined) > chars or len(combined.encode("utf-8")) > byte_count:
            raise BoardError("invalid_text_chunk")
        if next_offset is None:
            if (len(combined) != chars or len(combined.encode("utf-8")) != byte_count
                    or hashlib.sha256(combined.encode("utf-8")).hexdigest() != digest):
                raise BoardError("text_integrity")
            # Completion is committed before cache removal. A crash between them
            # leaves a ready message, never a lost body or a misleading partial.
            self.journal.save_body(entry["id"], combined, digest)
            with self.db:
                self.db.execute("DELETE FROM inbox_text_progress WHERE message_id=?", (entry["id"],))
        else:
            if type(next_offset) is not int or not offset < next_offset == len(combined) < chars:
                raise BoardError("invalid_text_chunk")
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO inbox_text_progress VALUES (?,?,?)",
                                (entry["id"], digest, combined))
        return True


class Coordinator:
    def __init__(self, store, journal, client, worker_state, request_quiesce,
                 lease=None, policy=Policy()):
        if journal.identity != client.identity or (lease and lease.client is not client):
            raise ValueError("Coordinator components must share one verified identity/client")
        self.store, self.journal, self.client, self.lease = store, journal, client, lease
        self.worker_state, self.request_quiesce, self.policy = worker_state, request_quiesce, policy
        self.inbox = CanonicalInbox(journal, client)
        self.next_poll = self.next_renew = self.next_text = 0
        self.state = store.get("coordination_control", {"mode": "idle", "offline_since": None,
            "stop_reason": None, "quiesce_request": None, "poll_failures": 0})

    def tick(self, now, backend):
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("A finite coordinator clock is required")
        if backend not in ("model_available", "waiting_for_model", "configuration_error"):
            raise ValueError("Unknown backend availability state")
        previous_mode = self.state["mode"]
        self.state["backend"] = backend
        self.state.setdefault("last_inbox_at", now)
        if backend == "model_available":
            self.state["offline_since"] = None
        elif self.state["offline_since"] is None:
            self.state["offline_since"] = now

        if now >= self.next_poll:
            try:
                count = self.journal.poll_once(self.client.read_page)
                previous_poll=self.state.get('last_inbox_at')
                self.state['poll_count']=self.state.get('poll_count',0)+1
                if previous_poll is not None:
                    self.state['max_poll_gap_seconds']=max(self.state.get('max_poll_gap_seconds',0),max(0,now-previous_poll))
                self.state.update(last_inbox_at=now, poll_failures=0, inbox_error=None)
                self.next_poll = now + (0.5 if count else self.policy.inbox_seconds)
            except (BoardError, JournalConflict):
                failures = min(self.state["poll_failures"] + 1, 10)
                self.state.update(poll_failures=failures, inbox_error="poll_requires_attention")
                self.next_poll = now + min(300, self.policy.inbox_seconds * 2 ** (failures - 1))

        worker = self.worker_state()
        if not isinstance(worker, WorkerState):
            raise ValueError("Engine adapter must return a verified WorkerState")
        saved_lease = self.lease.state() if self.lease else None
        has_work = worker.active or bool(saved_lease and (
            saved_lease["pending"] or (saved_lease["snapshot"] and saved_lease["snapshot"].get("claimant"))))
        reason = self.state["stop_reason"]
        if not reason and worker.active and self.lease is None:
            reason = "execution_without_selected_lease"
        if not reason and has_work and now < self.state.get("last_tick_at", now):
            reason = "clock_reconciliation_required"
        # `now` was sampled before this tick's inbox poll (which can take seconds through
        # the Mac tunnel) while the engine keeps refreshing progress_at, so only a large
        # lead means a real clock fault; regressions are caught by last_tick_at above.
        if not reason and worker.active and worker.progress_at > now + FUTURE_PROGRESS_TOLERANCE_SECONDS:
            reason = "clock_reconciliation_required"
        self.state["last_tick_at"] = now
        if not reason and saved_lease and (saved_lease["pending"] or saved_lease["fenced"]):
            reason = "lease_reconciliation_required"
        if not reason and has_work and now - self.state["last_inbox_at"] >= self.policy.control_stale_seconds:
            reason = "board_control_stale"
        if not reason and has_work and self.state["offline_since"] is not None:
            if now - self.state["offline_since"] >= self.policy.offline_grace_seconds:
                reason = "model_offline_grace_elapsed"
        if not reason and worker.active and now - worker.progress_at >= self.policy.stale_progress_seconds:
            reason = "stale_progress"
        self.state["stop_reason"] = reason
        if reason and not self.state["quiesce_request"]:
            request = {"id": uuid4().hex, "reason": reason, "requested_at": now}
            self.state["quiesce_request"] = request
            # Persist the intention before calling an engine; an uncertain pause
            # response must be reconciled, never blindly submitted again.
            self.store.save({"coordination_control": self.state})
            try:
                self.request_quiesce(request)
            except Exception:
                self.state["quiesce_error"] = "pause_outcome_requires_reconciliation"
                self.state["quiesce_retry_at"] = now + QUIESCE_RETRY_SECONDS
        elif (reason and self.state.get("quiesce_error") and worker.active
              and now >= self.state.get("quiesce_retry_at", 0)):
            # The same recorded intention, re-applied: stopping is idempotent (cancel
            # validation, stop the owned engine), whereas giving up leaves a worker
            # coding with no renewals. A long emulator run makes the first attempt
            # time out; later attempts succeed once that job has been cancelled.
            self.state["quiesce_retry_at"] = now + QUIESCE_RETRY_SECONDS
            self.store.save({"coordination_control": self.state})
            try:
                self.request_quiesce(self.state["quiesce_request"])
                self.state["quiesce_error"] = None
            except Exception:
                self.state["quiesce_error"] = "pause_outcome_requires_reconciliation"

        mode = "quiesce_requested" if reason else (
            "backend_configuration_error" if backend == "configuration_error" else (
                "waiting_for_model" if backend != "model_available" else ("running" if worker.active else "idle")))
        if self.lease:
            saved = self.lease.state()
            claimed = bool(saved["snapshot"] and saved["snapshot"].get("claimant"))
            if claimed and not (saved["pending"] or saved["fenced"]):
                if worker.quiescent and worker.cleanup_verified and worker.checkpoint and (reason or not worker.active):
                    try:
                        self.journal.save_checkpoint(self.lease.ticket_id, required_text(worker.engine_session), worker.checkpoint)
                        self.lease.release("Harness checkpoint: " + (reason or "execution stopped"), worker.release_status)
                        self.state["release_completed"] = True
                        mode = "released"
                    except (BoardError, JournalConflict):
                        mode = "reconciliation_required"
                elif worker.active and not reason and now >= self.next_renew:
                    try:
                        renewed = self.lease.renew("Harness verified active execution; coordination tick")
                        if renewed["lease_expires_at"] - now <= 60:
                            raise JournalConflict("Insufficient lease margin; stop instead of spinning renewals")
                        self.state["last_renew_at"] = now
                        self.next_renew = min(now + self.policy.renew_seconds, renewed["lease_expires_at"] - 60)
                    except (BoardError, JournalConflict):
                        self.state["stop_reason"] = "lease_reconciliation_required"
                        mode = "reconciliation_required"
                elif not worker.active:
                    mode = "stopped_requires_reconciliation"
            elif saved["pending"] or saved["fenced"]:
                mode = "reconciliation_required"
        if self.state.get("release_completed"):
            mode = "released"

        # Renewal/control decisions precede bounded body reads. A source error
        # cannot consume an unbounded number of requests or overwrite a preview.
        if now >= self.next_text:
            budget = self.policy.chunks_per_tick
            retries = self.state.setdefault("text_retry_at", {})
            entries = self.journal.pending()
            identifiers = [entry["id"] for entry in entries]
            last = self.state.get("last_text_message")
            if last in identifiers:
                start = identifiers.index(last) + 1
                entries = entries[start:] + entries[:start]
            for entry in entries:
                if now < retries.get(entry["id"], 0):
                    continue
                while (budget and entry["stage"] == "received"
                       and entry["envelope"].get("thread_id") in self.client.threads):
                    budget -= 1
                    self.state["last_text_message"] = entry["id"]
                    try:
                        self.inbox.step(entry)
                        retries.pop(entry["id"], None)
                        entry = self.journal._message(entry["id"])
                    except (BoardError, JournalConflict):
                        retries[entry["id"]] = now + self.policy.inbox_seconds
                        break
                if not budget:
                    break
            self.state["text_error"] = "canonical_source_requires_attention" if retries else None
            self.next_text = now + 0.5
        self.state["mode"] = mode
        event = ("coordination_mode_changed", {"mode": mode, "stop_reason": self.state["stop_reason"]}) if mode != previous_mode else None
        self.store.save({"coordination_control": self.state}, event)
        return dict(self.state)
