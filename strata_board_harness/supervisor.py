"""Keep local orchestration alive independently of the inference server.

The supervisor probes availability and composes optional selected Board/execution
runtime. Model listing is an availability signal, never proof of inference.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import random
import signal
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Config:
    backend_url: str
    model_id: str
    state_dir: Path
    api_key_env: str = "STRATA_API_KEY"
    poll_seconds: float = 5
    max_backoff_seconds: float = 60
    request_timeout_seconds: float = 5
    runtime_file: str | None = None

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        configured_state = data.pop("state_dir", None)
        state_dir = os.environ.get("STATE_DIRECTORY") or configured_state
        config = cls(state_dir=Path(state_dir or state_home / "strata-board-harness").expanduser(), **data)
        parsed = urlsplit(config.backend_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("backend_url must be an HTTP(S) base URL without embedded credentials or query")
        if not isinstance(config.model_id, str) or not config.model_id.strip():
            raise ValueError("model_id is required")
        for value in (config.poll_seconds, config.max_backoff_seconds, config.request_timeout_seconds):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("intervals must be finite positive numbers")
        if config.max_backoff_seconds < config.poll_seconds or config.request_timeout_seconds > 15:
            raise ValueError("backoff must cover polling; request timeout must be at most 15 seconds")
        return config


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "state.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL
            );
        """)

    def get(self, key, default=None):
        row = self.connection.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save(self, values, event=None):
        with self.connection:
            for key, value in values.items():
                self.connection.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
            if event:
                self.connection.execute("INSERT INTO events(at,kind,body) VALUES (?,?,?)",
                                        (utc_now(), event[0], json.dumps(event[1])))

    def close(self):
        self.connection.close()


class InstanceLock:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = (directory / "supervisor.lock").open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError("A supervisor already owns this state directory") from None
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()

    def close(self):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # An SSO redirect must not carry our bearer credential to another endpoint.
        return None


def probe(config):
    headers = {"Accept": "application/json"}
    key = os.environ.get(config.api_key_env)
    if key:
        headers["Authorization"] = "Bearer " + key
    request = Request(config.backend_url.rstrip("/") + "/models", headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=config.request_timeout_seconds) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                return {"state": "waiting_for_model", "detail": "oversized_models_response"}
            data = json.loads(body)
        models = data.get("data", [])
        if not isinstance(models, list):
            raise ValueError("invalid model list")
        for model in models:
            if not isinstance(model, dict) or model.get("id") != config.model_id:
                continue
            status = model.get("status")
            if isinstance(status, dict):
                status = status.get("value")
            if status and status not in ("loaded", "ready"):
                return {"state": "waiting_for_model", "detail": "model_not_loaded"}
            return {"state": "model_available", "detail": "advertised_by_models_endpoint"}
        return {"state": "waiting_for_model", "detail": "configured_model_not_listed"}
    except HTTPError as error:
        state = "configuration_error" if error.code in (301, 302, 303, 307, 308, 401, 403) else "waiting_for_model"
        code = error.code
        error.close()
        return {"state": state, "detail": "http_error", "http_status": code}
    except (URLError, TimeoutError, OSError):
        return {"state": "waiting_for_model", "detail": "backend_unreachable"}
    except (ValueError, AttributeError, UnicodeError):
        return {"state": "waiting_for_model", "detail": "invalid_models_response"}


class Supervisor:
    def __init__(self, config, store, probe_function=probe, jitter=None):
        self.config, self.store, self.probe_function = config, store, probe_function
        self.jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self.next_probe = 0.0
        self.next_heartbeat = 0.0
        self.failures = store.get("consecutive_failures", 0)

    def tick(self, monotonic):
        if monotonic >= self.next_heartbeat:
            self.store.save({"daemon": {"state": "running", "pid": os.getpid(), "heartbeat_at": utc_now()}})
            self.next_heartbeat = monotonic + 10
        if monotonic < self.next_probe:
            return
        result = self.probe_function(self.config)
        previous = self.store.get("backend", {})
        self.failures = 0 if result["state"] == "model_available" else min(self.failures + 1, 30)
        interval = min(self.config.max_backoff_seconds,
                       self.config.poll_seconds * 2 ** min(max(self.failures - 1, 0), 16))
        interval = min(self.config.max_backoff_seconds, interval * self.jitter())
        result = dict(result, checked_at=utc_now(), model_id=self.config.model_id)
        transition = previous.get("state") != result["state"] or previous.get("detail") != result["detail"]
        event = ("backend_state_changed", result) if transition else None
        self.store.save({"backend": result, "consecutive_failures": self.failures}, event)
        if transition:
            print(json.dumps({"event": "backend_state_changed", **result}), flush=True)
        self.next_probe = monotonic + interval


def run(config, once=False):
    lock = InstanceLock(config.state_dir)
    store = None
    runtime = None
    stop = threading.Event()
    previous_handlers = {}
    try:
        store = Store(config.state_dir)
        supervisor = Supervisor(config, store)
        if config.runtime_file:
            from .runtime import Runtime
            from .execution import from_config
            runtime = Runtime(store, config.runtime_file, worker_factory=from_config)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            supervisor.tick(time.monotonic())
            if runtime:
                runtime.tick(store.get("backend", {}).get("state", "waiting_for_model"))
            if once:
                break
            # Backend backoff does not suspend the local control/heartbeat loop.
            stop.wait(0.5)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if runtime:
            try: runtime.close()
            except Exception:
                if store: store.save({"shutdown": {"state": "cleanup_requires_reconciliation"}})
        if store:
            store.save({"daemon": {"state": "stopped", "pid": os.getpid(), "heartbeat_at": utc_now()}})
            store.close()
        lock.close()


def status(config):
    path = config.state_dir / "state.sqlite3"
    if not path.exists():
        return {"daemon": {"state": "not_started"}}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "kv" not in tables:
            if tables:
                raise RuntimeError("State database has an unexpected schema")
            # SQLite creates the file before Store's bootstrap DDL is committed.
            # A read-only status poll may observe that normal startup boundary.
            return {"daemon": {"state": "initializing", "detail": "state_schema_not_ready"}}
        return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM kv")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--once", action="store_true", help="One availability probe, with no task execution")
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if args.command == "status":
            print(json.dumps(status(config), indent=2))
        else:
            run(config, args.once)
        return 0
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        # Avoid echoing config values, HTTP bodies or credentials in service logs.
        print(json.dumps({"event": "supervisor_error", "error_type": type(error).__name__}), flush=True)
        return 1
