"""Bounded cold-session diagnostic with an isolated fake provider and owned PID."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from inspect_opencode import inspect
from probe_opencode_control import FixtureModel


def trace(args):
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(),
                "real_strata_requests": 0, "real_board_calls": 0, "snapshots": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with FixtureModel() as model:
        def configuration(root, work, env):
            model.work = work
            plugin = root / 'config/opencode/plugins'
            plugin.mkdir()
            shutil.copyfile(args.rtk_plugin, plugin / 'rtk.ts')
            (root / 'home/.config').symlink_to(root / 'config', target_is_directory=True)
            env['RTK_OPENCODE_AGENT'] = 'harness-fixture'
            return {"model": "fixture/fixture", "small_model": "fixture/fixture",
                    "provider": {"fixture": {"npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": f"http://127.0.0.1:{model.server.server_port}/v1", "apiKey": "fixture-only"},
                        "models": {"fixture": {"limit": {"context": 32768, "output": 2048}}}}},
                    "agent": {"harness-fixture": {"mode": "primary", "steps": 6}}}

        def exercise(request, root, work, api):
            pid = int((root / 'server.pid').read_text())
            stopped = threading.Event()
            def fingerprint():
                return subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=,args='],
                                      capture_output=True, text=True, timeout=3).stdout.strip()
            expected = fingerprint()
            def observe():
                for delay in [4, 8, 12]:
                    if stopped.wait(delay) or fingerprint() != expected:
                        return
                    snapshot = {'at': time.time(), 'pid': pid, 'process_fingerprint': expected}
                    paths = ['config/opencode/package.json', 'config/opencode/package-lock.json',
                             'config/opencode/node_modules', 'home/.npm/_logs']
                    snapshot['bootstrap_paths'] = {p: (root / p).exists() for p in paths}
                    package = root / paths[0]
                    if package.exists():
                        snapshot['fixture_package'] = json.loads(package.read_text())
                    # Only this fixture's npm diagnostic codes/statuses, never npmrc
                    # configuration or authorization headers.
                    snapshot['npm_signals'] = []
                    for log in (root / 'home/.npm/_logs').glob('*.log'):
                        for line in log.read_text(errors='replace').splitlines():
                            if re.search(r'\b(ETIMEDOUT|ECONNREFUSED|EAI_AGAIN|ENOTFOUND|E404|E403|E401)\b', line):
                                snapshot['npm_signals'].append(re.sub(r'https?://\S+', '[url]', line)[:500])
                    lsof = shutil.which('lsof')
                    if lsof:
                        r = subprocess.run([lsof, '-a', '-p', str(pid), '-i', '-n', '-P'],
                                           capture_output=True, text=True, timeout=4)
                        snapshot['network_files'] = r.stdout[-10000:]
                    sample = shutil.which('sample')
                    if sample and not evidence.get('stack_sample'):
                        file = root / 'owned-server.sample'
                        r = subprocess.run([sample, str(pid), '1', '-file', str(file)],
                                           capture_output=True, text=True, timeout=6)
                        evidence['stack_sample'] = {'exit_code': r.returncode,
                            'output': file.read_text(errors='replace')[-64000:] if file.exists() else r.stderr[-1000:]}
                    evidence['snapshots'].append(snapshot)
            observer = threading.Thread(target=observe, daemon=True)
            observer.start()
            start = time.monotonic()
            try:
                evidence['session'] = request('POST', '/session', {'title': 'Owned cold-start diagnostic'}, timeout=40)
                evidence['session_created'] = True
                return {'session_created': True}
            finally:
                evidence['session_request_seconds'] = time.monotonic() - start
                stopped.set()
                observer.join(timeout=12)
                evidence['observer_exited'] = not observer.is_alive()

        try:
            evidence['process_receipt'] = inspect(args.binary, args.worker_lock, args.output,
                args.api_output, configuration=configuration, exercise=exercise)
        except Exception as error:
            if args.output.exists():
                evidence['process_receipt'] = json.loads(args.output.read_text())
            evidence['error'] = {'type': type(error).__name__, 'message': str(error)}
        evidence['fake_model_requests'] = len(model.requests)
    evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({key: value for key, value in evidence.items()
                      if key not in ['snapshots', 'stack_sample', 'process_receipt']}, indent=2))
    return 0 if evidence.get('session_created') else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default='opencode')
    parser.add_argument('--rtk-plugin', type=Path, required=True)
    parser.add_argument('--worker-lock', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--api-output', type=Path, required=True)
    raise SystemExit(trace(parser.parse_args()))
