import contextlib
from dataclasses import replace
import io
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from strata_board_harness.supervisor import Config, InstanceLock, Store, Supervisor, probe, status


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.http_status = 200
        self.payload = {"data": [{"id": "fixture-model", "status": {"value": "loaded"}}]}
        self.paths = []
        self.authorization = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                owner.authorization = self.headers.get("Authorization")
                self.send_response(owner.http_status)
                if owner.http_status == 302:
                    self.send_header("Location", "/credential-trap")
                self.end_headers()
                self.wfile.write(json.dumps(owner.payload).encode())

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.config = Config(f"http://127.0.0.1:{self.server.server_port}/v1", "fixture-model",
                             self.directory / "state", api_key_env="HARNESS_TEST_API_KEY",
                             poll_seconds=0.1, max_backoff_seconds=0.5)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def write_config(self, **updates):
        data = {"backend_url": self.config.backend_url, "model_id": "fixture-model",
                "state_dir": str(self.config.state_dir), "api_key_env": "HARNESS_TEST_API_KEY",
                "poll_seconds": 0.1, "max_backoff_seconds": 0.5}
        data.update(updates)
        path = self.directory / "config.json"
        path.write_text(json.dumps(data))
        return path

    def test_authenticated_probe_checks_loaded_model(self):
        with patch.dict(os.environ, {"HARNESS_TEST_API_KEY": "fixture-private-key"}):
            self.assertEqual(probe(self.config)["state"], "model_available")
        self.assertEqual(self.authorization, "Bearer fixture-private-key")
        self.assertEqual(self.paths, ["/v1/models"])
        self.payload["data"][0]["status"]["value"] = "loading"
        self.assertEqual(probe(self.config)["state"], "waiting_for_model")

    def test_redirect_does_not_forward_credentials(self):
        self.http_status = 302
        with patch.dict(os.environ, {"HARNESS_TEST_API_KEY": "fixture-private-key"}):
            result = probe(self.config)
        self.assertEqual(result["state"], "configuration_error")
        self.assertEqual(self.paths, ["/v1/models"])
        self.assertNotIn("fixture-private-key", json.dumps(result))

    def test_outage_authentication_and_invalid_response_are_distinct(self):
        self.http_status = 503
        self.assertEqual(probe(self.config)["state"], "waiting_for_model")
        self.http_status = 401
        self.assertEqual(probe(self.config)["state"], "configuration_error")
        self.http_status = 200
        self.payload = ["not an OpenAI model list"]
        self.assertEqual(probe(self.config)["detail"], "invalid_models_response")
        self.payload = {"data": [{"id": "different-model"}]}
        self.assertEqual(probe(self.config)["detail"], "configured_model_not_listed")

    def test_outage_recovery_preserves_checkpoint_and_records_only_transitions(self):
        store = Store(self.config.state_dir)
        self.addCleanup(store.close)
        checkpoint = {"task": "fixture-task", "pending_messages": ["fixture-ping"], "commit": "fixture-head"}
        store.save({"checkpoint": checkpoint})
        results = iter([{"state": "model_available", "detail": "advertised_by_models_endpoint"},
                        {"state": "waiting_for_model", "detail": "backend_unreachable"},
                        {"state": "waiting_for_model", "detail": "backend_unreachable"},
                        {"state": "model_available", "detail": "advertised_by_models_endpoint"}])
        supervisor = Supervisor(self.config, store, lambda _: next(results), jitter=lambda: 1)
        with contextlib.redirect_stdout(io.StringIO()):
            for tick in (0, 1, 2, 3):
                supervisor.tick(tick)
        self.assertEqual(store.get("checkpoint"), checkpoint)
        self.assertEqual(store.get("backend")["state"], "model_available")
        self.assertEqual(store.get("consecutive_failures"), 0)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 3)

    def test_backoff_is_bounded_does_not_block_control_heartbeat_and_survives_restart(self):
        store = Store(self.config.state_dir)
        store.save({"consecutive_failures": 20, "pending": ["fixture-ping"]})
        config = replace(self.config, max_backoff_seconds=60, poll_seconds=5)
        supervisor = Supervisor(config, store,
                                lambda _: {"state": "waiting_for_model", "detail": "backend_unreachable"},
                                jitter=lambda: 1.2)
        with contextlib.redirect_stdout(io.StringIO()):
            supervisor.tick(0)
            self.assertEqual(supervisor.next_probe, 60)
            supervisor.tick(20)
        self.assertEqual(supervisor.next_heartbeat, 30)
        self.assertEqual(store.get("consecutive_failures"), 21)
        store.close()
        restarted = Store(self.config.state_dir)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.get("pending"), ["fixture-ping"])
        self.assertEqual(Supervisor(config, restarted).failures, 21)

    def test_lock_rejects_duplicate_supervisor_then_allows_restart(self):
        first = InstanceLock(self.config.state_dir)
        try:
            with self.assertRaises(RuntimeError):
                InstanceLock(self.config.state_dir)
        finally:
            first.close()
        restarted = InstanceLock(self.config.state_dir)
        restarted.close()

    def test_configuration_precedence_and_validation(self):
        path = self.write_config()
        with patch.dict(os.environ, {"STATE_DIRECTORY": str(self.directory / "systemd-state")}):
            self.assertEqual(Config.load(path).state_dir, self.directory / "systemd-state")
        for update in ({"backend_url": "https://user:secret@example.test/v1"},
                       {"backend_url": "https://example.test/v1?token=secret"},
                       {"poll_seconds": 0}, {"poll_seconds": float("nan")},
                       {"request_timeout_seconds": 16}, {"model_id": ""}):
            with self.subTest(update=list(update)), self.assertRaises(ValueError):
                Config.load(self.write_config(**update))

    def test_status_handles_empty_bootstrap_without_masking_an_unexpected_schema(self):
        self.config.state_dir.mkdir()
        path = self.config.state_dir / "state.sqlite3"
        with sqlite3.connect(path):
            pass
        self.assertEqual(status(self.config)["daemon"]["state"], "initializing")
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        store = Store(self.config.state_dir)
        try:
            store.save({"daemon": {"state": "fixture-ready"}})
            self.assertEqual(status(self.config)["daemon"]["state"], "fixture-ready")
        finally:
            store.close()
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE kv")
        with self.assertRaises(RuntimeError):
            status(self.config)

    def test_daemon_survives_model_shutdown_and_handles_sigterm(self):
        self.http_status = 503
        config_path = self.write_config()
        env = os.environ.copy()
        env.pop("STATE_DIRECTORY", None)
        env["HARNESS_TEST_API_KEY"] = "fixture-private-key"
        command = [sys.executable, "-m", "strata_board_harness", "--config", str(config_path), "run"]
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if status(self.config).get("backend", {}).get("state") == "waiting_for_model":
                    break
                if process.poll() is not None:
                    self.fail("supervisor exited while its model was unavailable")
                time.sleep(0.05)
            else:
                self.fail("supervisor never recorded the backend outage")
            self.assertIsNone(process.poll())
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(status(self.config)["daemon"]["state"], "stopped")
            self.http_status = 200
            result = subprocess.run(command + ["--once"], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status(self.config)["backend"]["state"], "model_available")
            self.assertNotIn("fixture-private-key", result.stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
