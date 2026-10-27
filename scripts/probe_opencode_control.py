"""Exercise the installed engine with a fake model and temporary owned tools.

No Strata, real Board, app worktree or physical/emulated device is contacted.
"""

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

from inspect_opencode import inspect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strata_board_harness.coordination import Identity, Journal
from strata_board_harness.engine import Binding, SelectedSession, command_results
from strata_board_harness.supervisor import Store


class FixtureModel:
    def __init__(self):
        self.requests = []
        self.main_count = self.stop_count = self.failure_count = 0
        self.work = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                record = {"at": time.time(), "path": self.path, "body": body,
                          "tool_finished": bool(owner.work and (owner.work / "finished").exists())}
                owner.requests.append(record)
                source = json.dumps(body)
                coding = bool(body.get("tools"))
                if coding and "HARNESS_CLIENT" in source:
                    match = re.search(r'GUIDANCE_ACK (msg_[A-Za-z0-9_-]+)', source)
                    command, answer = None, 'GUIDANCE_ACK ' + match.group(1) if match else 'MISSING_ACK_ID'
                elif coding and "HARNESS_FAIL" in source:
                    owner.failure_count += 1
                    command = "python3 fail_fixture.py" if owner.failure_count == 1 else None
                    answer = "FAILURE_REPORTED" if owner.failure_count > 1 else None
                elif coding and "HARNESS_STOP" in source:
                    owner.stop_count += 1
                    command = "python3 tool_stop_fixture.py" if owner.stop_count == 1 else None
                    answer = "STOP_CASE_CONTINUED" if owner.stop_count > 1 else None
                elif coding and "HARNESS_MAIN" in source:
                    owner.main_count += 1
                    command = {1: "python3 tool_fixture.py", 2: "ls"}.get(owner.main_count)
                    answer = "HARNESS_ACK" if owner.main_count >= 3 else None
                else:
                    command, answer = None, "Fixture title"
                call_id = "call_fixture_" + str(len(owner.requests))
                if command:
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": call_id,
                        "type": "function", "function": {"name": "bash", "arguments": json.dumps({
                            "command": command, "description": "Run owned harness fixture", "timeout": 20000})}}]}
                    finish = "tool_calls"
                else:
                    delta, finish = {"role": "assistant", "content": answer}, "stop"
                frames = [{"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": int(time.time()),
                           "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": int(time.time()),
                           "model": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for frame in frames:
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def probe(args):
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "real_inference_requests": 0,
                "real_board_calls": 0, "rtk_agent": "harness-fixture", "api_version": args.api_version}
    evidence["rtk_plugin_sha256"] = hashlib.sha256(args.rtk_plugin.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with FixtureModel() as model:
        def configuration(root, work, env):
            model.work = work
            plugins = root / "config/opencode/plugins"
            plugins.mkdir()
            shutil.copyfile(args.rtk_plugin, plugins / "rtk.ts")
            env["RTK_OPENCODE_AGENT"] = "harness-fixture"
            # RTK and OpenCode see the same temporary config even if a tool uses
            # HOME instead of XDG_CONFIG_HOME. Original config stays untouched.
            (root / "home/.config").symlink_to(root / "config", target_is_directory=True)
            env.pop("RTK_DISABLED", None)
            (work / "tool_fixture.py").write_text("""from pathlib import Path
import os, time
Path('main_pid').write_text(str(os.getpid()))
Path('started').write_text('owned fixture')
deadline=time.monotonic()+15
while not Path('release').exists() and time.monotonic()<deadline: time.sleep(0.05)
if not Path('release').exists(): raise SystemExit(7)
Path('finished').write_text('completed before next inference')
print('fixture tool completed')
""")
            (work / "tool_stop_fixture.py").write_text("""from pathlib import Path
import os, time
Path('stop_pid').write_text(str(os.getpid()))
Path('stop_started').write_text('owned fixture')
time.sleep(15)
Path('stop_finished').write_text('would indicate failed interruption')
""")
            (work / "fail_fixture.py").write_text("print('BUILD FAILED (owned fixture)')\nraise SystemExit(37)\n")
            hook = subprocess.run(["rtk", "hook", "opencode", "--agent", "harness-fixture", "ls"],
                                  cwd=work, env=env, text=True, capture_output=True, timeout=10)
            evidence["rtk_hook"] = json.loads(hook.stdout) if hook.returncode == 0 else {"exit_code": hook.returncode}
            return {"model": "fixture/fixture", "small_model": "fixture/fixture",
                "provider": {"fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Owned fixture",
                    "options": {"baseURL": f"http://127.0.0.1:{model.server.server_port}/v1", "apiKey": "fixture-only"},
                    "models": {"fixture": {"name": "Fixture", "limit": {"context": 32768, "output": 2048}}}}},
                "agent": {"harness-fixture": {"mode": "primary", "steps": 6}},
                "permission": {"*": "deny", "bash": {"*": "deny", "python3 tool_fixture.py": "allow",
                    "python3 tool_stop_fixture.py": "allow", "python3 fail_fixture.py": "allow",
                    "ls": "allow", "rtk ls*": "allow"},
                    "external_directory": "deny"}}

        def exercise(request, root, work, api):
            owned_tools = {}
            server_pid = int((root / "server.pid").read_text())
            def fingerprint(pid):
                return subprocess.run(["ps", "-p", str(pid), "-o", "lstart=,args="],
                                      capture_output=True, text=True, timeout=3).stdout.strip()

            def descends_from_server(pid):
                for _ in range(32):
                    if pid == server_pid:
                        return True
                    parent = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="],
                                            capture_output=True, text=True, timeout=3).stdout.strip()
                    if not parent.isdigit() or int(parent) <= 1:
                        return False
                    pid = int(parent)
                return False

            server_fingerprint = fingerprint(server_pid)

            def observe_tool(marker):
                path = work / marker
                if path.exists():
                    pid = int(path.read_text())
                    if pid in owned_tools:
                        return pid
                    command = fingerprint(pid)
                    if command and descends_from_server(pid):
                        owned_tools[pid] = command
                    elif command:
                        raise RuntimeError("Refusing a fixture PID outside the owned server process tree")
                    return pid
                return None

            def exited(pid):
                current = fingerprint(pid)
                return not current or (pid in owned_tools and current != owned_tools[pid])

            def wait_for(predicate, description, seconds=25):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.05)
                raise RuntimeError("Fixture deadline: " + description)

            def session():
                if args.api_version == "v1":
                    return request("POST", "/session", {"title": "Owned harness control fixture"}, timeout=30)["id"]
                return request("POST", "/api/session", {"agent": "harness-fixture",
                    "model": {"providerID": "fixture", "id": "fixture"},
                    "location": {"directory": str(work)}})["data"]["id"]

            def prompt(sid, text, delivery="steer", resume=True):
                mid = "msg_" + secrets.token_hex(16)
                if args.api_version == "v1":
                    request("POST", f"/session/{sid}/prompt_async", {"messageID": mid,
                        "agent": "harness-fixture", "model": {"providerID": "fixture", "modelID": "fixture"},
                        "noReply": not resume, "parts": [{"type": "text", "text": text}]})
                    return {"id": mid, "http_accepted": True, "applied": False}
                return request("POST", f"/api/session/{sid}/prompt", {"id": mid,
                    "prompt": {"text": text}, "delivery": delivery, "resume": resume})["data"]

            def history(sid):
                path = f"/session/{sid}/message" if args.api_version == "v1" else f"/api/session/{sid}/history?limit=100"
                return request("GET", path)

            def assistant_text_seen(sid, text):
                rows = history(sid)
                if args.api_version == "v1":
                    return any(item.get("info", {}).get("role") == "assistant" and any(
                        part.get("type") == "text" and part.get("text") == text
                        for part in item.get("parts", [])) for item in rows)
                return any(item.get("type") == "session.next.text.ended" and item.get("data", {}).get("text") == text
                           for item in rows.get("data", []))

            def tool_states(rows):
                if args.api_version == "v1":
                    return [part["state"] for item in rows for part in item.get("parts", [])
                            if part.get("type") == "tool" and part.get("tool") == "bash"]
                return []

            try:
                evidence["stage"] = "creating_main_session"
                first = session()
                evidence["main_session"] = first
                evidence["first_admission"] = prompt(first, "HARNESS_MAIN: run the owned fixture tool, then inspect ls and acknowledge.")
                wait_for(lambda: (work / "started").exists(), "owned tool start")
                observe_tool("main_pid")
                evidence["ping_admission"] = prompt(first, "HARNESS_PING: finish the current owned tool, then ls and HARNESS_ACK.")
                time.sleep(0.3)
                evidence["main_requests_before_tool_release"] = model.main_count
                (work / "release").write_text("owned coordinator release")
                # Installed 1.19.3 /wait returns a documented ServiceUnavailable
                # placeholder. Observe actual model/history completion instead.
                wait_for(lambda: model.main_count >= 3, "model continuation after owned tool")
                wait_for(lambda: assistant_text_seen(first, "HARNESS_ACK"), "actual assistant acknowledgement")
                main_history = history(first)
                evidence["main_history"] = main_history
                evidence["main_model_requests"] = model.main_count
                evidence["main_tool_finished"] = (work / "finished").exists()
                evidence["rtk_in_engine_history"] = any(state.get("status") == "completed"
                    and state.get("input", {}).get("command") == "rtk ls"
                    and state.get("metadata", {}).get("exit") == 0 for state in tool_states(main_history))
                evidence["ping_seen_by_model"] = any("HARNESS_PING" in json.dumps(item["body"])
                    for item in model.requests if item["tool_finished"] and item["body"].get("tools"))
                evidence["model_waited_for_tool"] = evidence["main_requests_before_tool_release"] == 1

                second = session()
                evidence["stop_session"] = second
                evidence["stop_admission"] = prompt(second, "HARNESS_STOP: run the owned stop fixture.")
                wait_for(lambda: (work / "stop_started").exists(), "owned stop tool start")
                stop_pid = observe_tool("stop_pid")
                evidence["queued_admission"] = prompt(second, "HARNESS_STOP pending guidance; must stay dormant after interrupt.", "queue", False)
                request("POST", f"/session/{second}/abort" if args.api_version == "v1" else f"/api/session/{second}/interrupt")
                wait_for(lambda: exited(stop_pid), "owned tool process exit after interrupt", 5)
                time.sleep(0.5)
                evidence["stop_model_requests_after_interrupt"] = model.stop_count
                evidence["stop_tool_finished"] = (work / "stop_finished").exists()
                evidence["stop_tool_process_exited"] = exited(stop_pid)
                evidence["stop_history"] = history(second)
                evidence["interrupt_stayed_stopped"] = model.stop_count == 1 and not evidence["stop_tool_finished"]
                evidence["post_interrupt_observation_seconds"] = 0.5
                failure = session()
                evidence["failure_session"] = failure
                prompt(failure, "HARNESS_FAIL: run owned failure fixture and report the failure.")
                wait_for(lambda: assistant_text_seen(failure, "FAILURE_REPORTED"), "failure fixture report")
                failure_history = history(failure)
                evidence["failure_history"] = failure_history
                evidence["failed_exit_preserved"] = any(state.get("status") == "completed"
                    and state.get("input", {}).get("command") == "python3 fail_fixture.py"
                    and state.get("metadata", {}).get("exit") == 37 for state in tool_states(failure_history))
                evidence['client_exit37_receipt'] = any(result['exit_code'] == 37 and not result['passed']
                    and 'BUILD FAILED' in result['output']
                    for row in failure_history for result in command_results(row)) if args.api_version == 'v1' else False
                if args.api_version == 'v1':
                    class InstalledTransport:
                        expected_version = '1.19.3'
                        def request(self, method, path, payload=None):
                            return request(method, path, payload, with_status=True)

                    selected = session()
                    store = Store(root / 'client-journal')
                    try:
                        identity = Identity('fixture-owner', 'fixture-worker', 'fixture-device', 'fixture-session')
                        journal = Journal(store, identity)
                        fixture_session = {'owner_id': identity.owner_id, 'agent_id': identity.agent_id,
                            'device_id': identity.device_id, 'id': identity.session_id}
                        journal.persist_page(None, {'session': fixture_session, 'cursor': 'fixture-cursor',
                            'messages': [{'id': 'fixture-ping', 'author_id': 'fixture-manager', 'thread_id': 'fixture-thread'}]})
                        body = 'HARNESS_CLIENT: retain the selected task scope and acknowledge this guidance.'
                        journal.save_body('fixture-ping', body, hashlib.sha256(body.encode()).hexdigest())
                        client = SelectedSession(journal, InstalledTransport(),
                            Binding('fixture-task', selected, str(work), 'harness-fixture', 'fixture', 'fixture'),
                            ['fixture-task'], lambda: fingerprint(server_pid) == server_fingerprint,
                            lambda message, body, task: message['author_id'] == 'fixture-manager'
                                and message['thread_id'] == 'fixture-thread' and task == 'fixture-task')
                        admission = client.deliver('fixture-ping')
                        evidence['client_admission'] = admission
                        marker = 'GUIDANCE_ACK ' + admission['request_id']
                        wait_for(lambda: assistant_text_seen(selected, marker), 'scoped client assistant acknowledgement')
                        client_history = history(selected)
                        assistant = next(row['info']['id'] for row in client_history
                            if row['info'].get('role') == 'assistant' and any(
                                part.get('text') == marker for part in row.get('parts', [])))
                        evidence['client_acknowledgement'] = client.acknowledge('fixture-ping', assistant)
                        evidence['client_reconciled'] = client.reconcile_delivery('fixture-ping')
                        evidence['client_receipts'] = journal.receipts('fixture-ping')
                        evidence['client_history'] = client_history
                        evidence['scoped_client_verified'] = (journal.pending()[0]['stage'] == 'acknowledged'
                            and evidence['client_acknowledgement'] == {'acknowledged': True, 'applied': False}
                            and evidence['client_reconciled'] == {'observed': True, 'acknowledged': True, 'applied': False})
                    finally:
                        store.close()
                evidence["passed"] = all(evidence.get(key) for key in ["model_waited_for_tool",
                    "main_tool_finished", "ping_seen_by_model", "rtk_in_engine_history", "interrupt_stayed_stopped",
                    "stop_tool_process_exited", "failed_exit_preserved", 'client_exit37_receipt', 'scoped_client_verified'])
                return {key: value for key, value in evidence.items() if not key.endswith("history")}
            finally:
                observe_tool("main_pid")
                observe_tool("stop_pid")
                for pid, expected_fingerprint in owned_tools.items():
                    current = fingerprint(pid)
                    if current and current == expected_fingerprint:
                        os.kill(pid, signal.SIGTERM)
                        wait_for(lambda: exited(pid), "owned fixture tool cleanup", 5)
                evidence["owned_tool_processes_exited"] = all(exited(pid) for pid in owned_tools)

        try:
            result = inspect(args.binary, args.worker_lock, args.output, args.api_output,
                             configuration=configuration, exercise=exercise)
            evidence["process_receipt"] = result
        except Exception as error:
            if args.output.exists():
                evidence["process_receipt"] = json.loads(args.output.read_text())
            evidence["passed"] = False
            evidence["error"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            evidence["fixture_requests"] = model.requests
            args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({key: value for key, value in evidence.items()
                      if key not in ("main_history", "stop_history", "failure_history", 'client_history', "fixture_requests", "process_receipt")}, indent=2))
    return 0 if evidence.get("passed") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="opencode")
    parser.add_argument("--rtk-plugin", type=Path, required=True)
    parser.add_argument("--worker-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-output", type=Path, required=True)
    parser.add_argument("--api-version", choices=["v1", "v2"], default="v1")
    raise SystemExit(probe(parser.parse_args()))
