"""Durable inbox journal, independent of inference and live Board credentials.

This module records evidence; it does not authorize guidance, claim tasks, submit
engine requests or infer successful application from a queued acknowledgement.
"""

from dataclasses import asdict, dataclass
import hashlib
import json

from .supervisor import utc_now


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty identifier or receipt is required")
    return value


class JournalConflict(RuntimeError):
    """Local or remote evidence changed; reconcile before advancing."""


@dataclass(frozen=True)
class Identity:
    owner_id: str
    agent_id: str
    device_id: str
    session_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            required_text(value)

    def matches(self, session):
        return isinstance(session, dict) and all(
            session.get("id" if key == "session_id" else key) == value
            for key, value in asdict(self).items())


class Journal:
    def __init__(self, store, identity):
        self.store = store
        self.db, self.identity = store.connection, identity
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS coord_reader (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                identity TEXT NOT NULL, cursor TEXT
            );
            CREATE TABLE IF NOT EXISTS coord_messages (
                id TEXT PRIMARY KEY, envelope TEXT NOT NULL, received_at TEXT NOT NULL,
                body TEXT, body_sha256 TEXT, stage TEXT NOT NULL DEFAULT 'received',
                engine_session TEXT, request_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS coord_receipts (
                message_id TEXT NOT NULL, phase TEXT NOT NULL, at TEXT NOT NULL,
                evidence TEXT NOT NULL, PRIMARY KEY(message_id,phase)
            );
            CREATE TABLE IF NOT EXISTS coord_checkpoints (
                task_id TEXT PRIMARY KEY, engine_session TEXT NOT NULL,
                payload TEXT NOT NULL, saved_at TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO coord_reader VALUES (1,?,NULL)",
                            (encoded(asdict(identity)),))
            saved = self.db.execute("SELECT identity FROM coord_reader WHERE singleton=1").fetchone()[0]
            if saved != encoded(asdict(identity)):
                raise JournalConflict("State belongs to a different Board reader")

    def cursor(self):
        return self.db.execute("SELECT cursor FROM coord_reader WHERE singleton=1").fetchone()[0]

    def poll_once(self, read_page):
        """Read one compact page. Persist it before its cursor is reused as an ack.

        The injected reader owns transport/authentication. No model is called.
        Failure before commit leaves the previous cursor available for replay.
        """
        expected = self.cursor()
        return self.persist_page(expected, read_page(expected))

    def persist_page(self, expected_cursor, page):
        if not isinstance(page, dict) or not self.identity.matches(page.get("session")):
            raise JournalConflict("Inbox response identity does not match this reader")
        cursor = required_text(page.get("cursor"))
        messages = page.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Inbox messages must be a list")
        inserted = 0
        with self.db:
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("Inbox messages must be objects")
                message_id = required_text(message.get("id"))
                envelope = encoded(message)
                saved = self.db.execute("SELECT envelope FROM coord_messages WHERE id=?",
                                        (message_id,)).fetchone()
                if saved and saved[0] != envelope:
                    raise JournalConflict("A replay changed a saved message; reconcile its source")
                if not saved:
                    self.db.execute("INSERT INTO coord_messages(id,envelope,received_at) VALUES (?,?,?)",
                                    (message_id, envelope, utc_now()))
                    inserted += 1
            changed = self.db.execute("UPDATE coord_reader SET cursor=? WHERE singleton=1 AND cursor IS ?",
                                      (cursor, expected_cursor)).rowcount
            if changed != 1:
                raise JournalConflict("Inbox cursor changed during polling")
        return inserted

    def save_body(self, message_id, body, expected_sha256):
        """Accept fully fetched source only after checking its canonical reference hash."""
        if not isinstance(body, str):
            raise ValueError("Canonical body must be text")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest != expected_sha256:
            raise JournalConflict("Canonical body hash does not match its source reference")
        with self.db:
            row = self._message(message_id)
            if row["body_sha256"] and (row["body_sha256"] != digest or row["body"] != body):
                raise JournalConflict("Canonical source changed; reconcile before delivery")
            if row["stage"] == "received":
                self.db.execute("UPDATE coord_messages SET body=?,body_sha256=?,stage='ready' WHERE id=?",
                                (body, digest, message_id))

    def prepare_delivery(self, message_id, engine_session, request_id):
        """Persist an intention BEFORE external submission, using a stable request ID.

        After a crash, prepared/submitted entries need engine reconciliation.
        Never blindly resubmit them or interpret this intention as delivery.
        Authorization and safe-boundary checks belong to the future coordinator.
        """
        required_text(engine_session)
        required_text(request_id)
        with self.db:
            row = self._message(message_id)
            if row["engine_session"]:
                if (row["engine_session"], row["request_id"]) != (engine_session, request_id):
                    raise JournalConflict("Delivery already has a different stable identity")
            elif row["stage"] != "ready":
                raise JournalConflict("Canonical source is required before delivery preparation")
            else:
                self.db.execute("UPDATE coord_messages SET engine_session=?,request_id=?,stage='prepared' WHERE id=?",
                                (engine_session, request_id, message_id))
        return self._message(message_id)

    def record_receipt(self, message_id, phase, evidence):
        preceding = {"submitted": "prepared", "acknowledged": "submitted", "applied": "acknowledged"}
        if phase not in preceding or not isinstance(evidence, dict) or not evidence:
            raise ValueError("A recognized phase and nonempty evidence object are required")
        payload = encoded(evidence)
        with self.db:
            row = self._message(message_id)
            saved = self.db.execute("SELECT evidence FROM coord_receipts WHERE message_id=? AND phase=?",
                                    (message_id, phase)).fetchone()
            if saved:
                if saved[0] != payload:
                    raise JournalConflict("Receipt replay changed its evidence")
                return
            if row["stage"] != preceding[phase]:
                raise JournalConflict("Delivery receipts must follow observed execution order")
            self.db.execute("INSERT INTO coord_receipts VALUES (?,?,?,?)",
                            (message_id, phase, utc_now(), payload))
            self.db.execute("UPDATE coord_messages SET stage=? WHERE id=?", (phase, message_id))

    def pending(self):
        return [self._message(row[0]) for row in self.db.execute(
            "SELECT id FROM coord_messages WHERE stage!='applied' ORDER BY rowid")]

    def receipts(self, message_id):
        self._message(message_id)
        return [{"phase": phase, "at": at, "evidence": json.loads(evidence)}
                for phase, at, evidence in self.db.execute(
                    "SELECT phase,at,evidence FROM coord_receipts WHERE message_id=? ORDER BY rowid",
                    (message_id,))]

    def save_checkpoint(self, task_id, engine_session, payload):
        required_text(task_id)
        required_text(engine_session)
        if not isinstance(payload, dict):
            raise ValueError("Checkpoint must be an object")
        with self.db:
            saved = self.db.execute("SELECT engine_session FROM coord_checkpoints WHERE task_id=?",
                                    (task_id,)).fetchone()
            if saved and saved[0] != engine_session:
                raise JournalConflict("Task checkpoint belongs to a different engine session")
            self.db.execute("INSERT OR REPLACE INTO coord_checkpoints VALUES (?,?,?,?)",
                            (task_id, engine_session, encoded(payload), utc_now()))

    def checkpoint(self, task_id):
        row = self.db.execute("SELECT engine_session,payload,saved_at FROM coord_checkpoints WHERE task_id=?",
                              (task_id,)).fetchone()
        return {"engine_session": row[0], "payload": json.loads(row[1]), "saved_at": row[2]} if row else None

    def _message(self, message_id):
        cursor = self.db.execute("SELECT * FROM coord_messages WHERE id=?", (message_id,))
        row = cursor.fetchone()
        if not row:
            raise JournalConflict("Message was not received by this reader")
        result = dict(zip([column[0] for column in cursor.description], row))
        result["envelope"] = json.loads(result["envelope"])
        return result
