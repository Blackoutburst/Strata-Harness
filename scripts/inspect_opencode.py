"""Inspect an installed OpenCode API in an isolated, authenticated temporary home.

The CLI submits no model prompt, Board connection or worker task. An optional
fixture callback can exercise a fake model; it must never select real tasks.
Pure server mode is API inspection only, not the RTK-enabled developer pilot.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

def stop_server(child):
    """Stop only the process group created by this inspection invocation."""
    if child is None or child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def inspect(binary, lock, output, api_output, configuration=None, exercise=None):
    processes = subprocess.check_output(["ps", "-axo", "pid=,comm="], text=True)
    for line in processes.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and Path(fields[1]).name == "opencode":
            raise RuntimeError("An OpenCode process already exists; inspect it before probing")
    lock.mkdir()  # Refuse any existing cross-model worker lock; never clear it.
    owner = {"pid": os.getpid(), "nonce": secrets.token_hex(16), "purpose": "isolated_api_inspection"}
    owner_file = lock / "owner.json"
    owner_file.write_text(json.dumps(owner))
    (lock / "pid").write_text(str(os.getpid()))
    child = None
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "real_inference_requests": 0,
              "real_board_calls": 0, "pure_api_inspection_only": configuration is None}
    try:
        with tempfile.TemporaryDirectory(prefix="strata-opencode-api-") as temporary:
            # OpenCode canonicalizes its workspace. Give config/cache/home the
            # same spelling (macOS /var is a symlink to /private/var).
            root = Path(temporary).resolve()
            env = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")}
            env["HOME"] = str(root / "home")
            for key in ("CONFIG", "DATA", "STATE", "CACHE"):
                env["XDG_" + key + "_HOME"] = str(root / key.lower())
            env.update(OPENCODE_DISABLE_MODELS_FETCH="1", OPENCODE_DISABLE_AUTOUPDATE="1",
                       OPENCODE_DISABLE_DEFAULT_PLUGINS="1", OPENCODE_DISABLE_LSP_DOWNLOAD="1",
                       OPENCODE_DISABLE_PROJECT_CONFIG="1",
                       OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER="1",
                       OPENCODE_SERVER_USERNAME="harness-probe", OPENCODE_SERVER_PASSWORD=secrets.token_urlsafe(24))
            result["default_plugins_disabled"] = True
            result["project_config_disabled"] = True
            result["fixture_filewatcher_disabled"] = True
            config = root / "config/opencode"
            config.mkdir(parents=True)
            (root / "home").mkdir()
            work = root / "workspace"
            work.mkdir()
            settings = {"autoupdate": False, "share": "disabled", "mcp": {}, "permission": "deny"}
            if configuration:
                settings.update(configuration(root, work, env))
            (config / "opencode.json").write_text(json.dumps(settings))
            result["binary_version"] = subprocess.check_output([binary, "--version"], env=env, text=True, timeout=10).strip()
            log_path = root / "server.log"
            with log_path.open("w+") as log:
                child = subprocess.Popen([binary, "serve", *([] if configuration else ["--pure"]), "--hostname", "127.0.0.1", "--port", "0",
                                          "--print-logs", "--log-level", "ERROR"], cwd=work, env=env,
                                         stdout=log, stderr=log, start_new_session=True)
                try:
                    result["server_pid"] = child.pid
                    result["temporary_root"] = str(root)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps(result, indent=2) + "\n")
                    (root / "server.pid").write_text(str(child.pid))
                    deadline = time.monotonic() + 30
                    url = None
                    while time.monotonic() < deadline and child.poll() is None:
                        match = re.search(r"http://127\.0\.0\.1:\d+", log_path.read_text())
                        if match:
                            url = match.group(0)
                            break
                        time.sleep(0.1)
                    if not url:
                        raise RuntimeError("Isolated OpenCode did not announce a loopback API")
                    auth = base64.b64encode((env["OPENCODE_SERVER_USERNAME"] + ":" + env["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
                    def get(path, authenticated=True):
                        request = Request(url + path, headers={"Authorization": "Basic " + auth} if authenticated else {})
                        with urlopen(request, timeout=10) as response:
                            return json.loads(response.read(4 * 1024 * 1024))
                    try:
                        get("/global/health", authenticated=False)
                        raise RuntimeError("OpenCode API authentication was not enforced")
                    except HTTPError as error:
                        result["unauthenticated_status"] = error.code
                        error.close()
                        if error.code != 401:
                            raise RuntimeError("Unexpected unauthenticated health response") from None
                    result["health"] = get("/global/health")
                    api = get("/doc")
                    api_output.parent.mkdir(parents=True, exist_ok=True)
                    api_output.write_text(json.dumps(api, indent=2) + "\n")
                    result["api_schema"] = str(api_output)
                    result["paths"] = {path: list(methods) for path, methods in api["paths"].items()
                                       if "session" in path or path in ("/event", "/global/event", "/global/health")}
                    if exercise:
                        def request(method, path, payload=None, timeout=15, with_status=False):
                            headers = {"Authorization": "Basic " + auth}
                            if payload is not None:
                                headers["Content-Type"] = "application/json"
                            data = json.dumps(payload).encode() if payload is not None else None
                            try:
                                with urlopen(Request(url + path, data=data, headers=headers, method=method), timeout=timeout) as response:
                                    body = response.read(4 * 1024 * 1024)
                                    value = json.loads(body) if body else None
                                    return (response.status, value) if with_status else value
                            except HTTPError as error:
                                detail = error.read(65536).decode("utf-8", errors="replace")
                                status = error.code
                                error.close()
                                raise RuntimeError(f"Isolated fixture API {method} {path}: HTTP {status}: {detail}") from None
                        try:
                            result["exercise"] = exercise(request, root, work, api)
                        finally:
                            result["fixture_server_log"] = log_path.read_text()[-32000:]
                finally:
                    # Stop the owned server before deleting its workspace/cache.
                    stop_server(child)

    finally:
        stop_server(child)
        result["server_exited"] = child is None or child.poll() is not None
        result["temporary_files_removed"] = "root" not in locals() or not root.exists()
        if result["server_exited"] and json.loads(owner_file.read_text()) == owner:
            owner_file.unlink()
            (lock / "pid").unlink()
            lock.rmdir()
        result["owned_worker_lock_removed"] = not lock.exists()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="opencode")
    parser.add_argument("--worker-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.binary, args.worker_lock, args.output, args.api_output), indent=2))
