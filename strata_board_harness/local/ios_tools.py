"""`harness-tool build` / `harness-tool test` for iOS agents.

The agent never drives xcodebuild or simctl itself (both are denied to it). A test run:
- resets the agent's own simulator (shutdown + erase + boot), the iOS counterpart of the
  Android snapshot restore, so every run starts from a clean device;
- starts the repository's host helper, if the project has one, and always stops it;
- runs `xcodebuild test` with per-test time allowances, so one hung test cannot eat the run;
- reads the result bundle and returns a short summary in the same shape as the Android tool.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

MIN_FREE_BYTES = 15 * 2**30
CLASS_FILTER = re.compile(r'class:([A-Za-z0-9_]+/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+(?:\(\))?)?)')


def parse_test_results(document):
    """Test cases from `xcresulttool get test-results tests` JSON: name, result and messages."""
    cases = []

    def walk(node, suite):
        kind = node.get('nodeType')
        if kind == 'Test Case':
            messages = [child.get('name', '') for child in node.get('children', [])
                        if child.get('nodeType') in ('Failure Message', 'Skip Message', 'Expected Failure')]
            cases.append({'class': suite, 'test': (node.get('name') or '').removesuffix('()'),
                          'result': node.get('result') or 'Unknown', 'messages': messages})
            return
        for child in node.get('children', []):
            walk(child, node.get('name') if kind == 'Test Suite' else suite)

    for node in document.get('testNodes', []):
        walk(node, None)
    return cases


def summarize(cases):
    counts = {'declared_total': len(cases), 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0,
              'unrun': 0, 'crashed': False}
    lines = []
    for case in cases:
        result = case['result'].lower()
        name = f"{case['class']}/{case['test']}"
        if result == 'passed':
            counts['passed'] += 1
        elif result == 'skipped':
            counts['skipped'] += 1
            lines.append(f"SKIP {name}: {' | '.join(case['messages'])[:400]}")
        elif result in ('failed', 'expected failure'):
            counts['failed'] += 1
            lines.append(f"FAIL {name}: {' | '.join(case['messages'])[:600]}")
            if any('crashed' in message.lower() for message in case['messages']):
                counts['crashed'] = True
        else:
            counts['unrun'] += 1
    if counts['failed']:
        lines.append(f"Tests run: {len(cases)}, Failures: {counts['failed']}, Skipped: {counts['skipped']}")
    else:
        lines.append(f"OK ({counts['passed']} tests, {counts['skipped']} skipped)")
    return counts, lines


class IosTools:
    def __init__(self, agent, workspace, run_dir, env):
        self.agent, self.workspace, self.env = agent, Path(workspace).resolve(), dict(env)
        self.config = agent.config['tools']['simulator']
        self.run_dir = Path(run_dir).resolve()
        # Kept across runs so builds are incremental; the agent's checkout stays clean.
        self.derived = agent.dir / 'ios-derived-data'

    def handlers(self):
        return {'build': self.build, 'test': self.test}

    def xcodebuild(self, action, extra=()):
        return ['xcodebuild', '-project', self.config['project'], '-scheme', self.config['scheme'],
                '-destination', f"platform=iOS Simulator,id={self.config['udid']}",
                '-derivedDataPath', str(self.derived), *self.signing(), *extra, action]

    def signing(self):
        # Simulator builds sign ad hoc by default (no certificate, no account), which keeps the app's
        # entitlements: keychain access groups, app groups, extensions. Stripping signing breaks any
        # app that uses its keychain, so it is an explicit per-project opt-out.
        return ['CODE_SIGNING_ALLOWED=NO'] if self.config.get('unsigned') else []

    def run(self, command, cancelled, cap, env=None):
        log = []
        child = subprocess.Popen(command, cwd=self.workspace, env=env or self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
        reader = threading.Thread(target=lambda: log.extend(child.stdout), daemon=True)
        reader.start()
        deadline, reason = time.monotonic() + cap, None
        while child.poll() is None:
            if cancelled.wait(.5):
                reason = 'cancelled'
            elif time.monotonic() >= deadline:
                reason = 'timeout'
            if reason:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                break
        code = child.wait(timeout=60)
        reader.join(timeout=5)
        return (124 if reason == 'timeout' else 130 if reason == 'cancelled' else code), ''.join(log), reason

    def simctl(self, *args, check=False):
        result = subprocess.run(['xcrun', 'simctl', *args], capture_output=True, text=True, timeout=300)
        if check and result.returncode:
            raise RuntimeError(f"simctl {' '.join(args)} failed: {result.stderr.strip()[-400:]}")
        return result

    def build(self, argument, cancelled):
        code, output, reason = self.run(self.xcodebuild('build-for-testing'), cancelled, 1800)
        return {'exit_code': code, 'output': output, 'cleanup_verified': True, 'stop_reason': reason}

    def reset_simulator(self):
        udid = self.config['udid']
        self.simctl('shutdown', udid)
        self.simctl('erase', udid, check=True)
        self.simctl('boot', udid, check=True)
        self.simctl('bootstatus', udid, '-b', check=True)

    def helper(self, key):
        script = self.config.get(key)
        if not script:
            return ''
        try:
            result = subprocess.run([str(self.workspace / script)], cwd=self.workspace, capture_output=True,
                                    text=True, timeout=120, env={**self.env, 'PROJECT_DIR': str(self.workspace)})
        except (OSError, subprocess.TimeoutExpired) as error:
            return f'[{key}] failed: {error}'
        return f'[{key}] exit {result.returncode}\n{result.stdout[-800:]}{result.stderr[-800:]}'

    def test(self, argument, cancelled):
        free = shutil.disk_usage(Path.home()).free
        if free < MIN_FREE_BYTES:
            return {'exit_code': 75, 'cleanup_verified': True,
                    'output': f'Refused: only {free // 2**30} GB free on disk (needs {MIN_FREE_BYTES // 2**30} GB).'}
        extra = []
        if argument:
            match = CLASS_FILTER.fullmatch(argument)
            if not match:
                return {'exit_code': 2, 'cleanup_verified': True,
                        'output': 'Use `harness-tool test` or `harness-tool test class:<Target>/<Class>[/<test>]`.'}
            extra.append(f'-only-testing:{match.group(1)}')
        allowance = int(self.config.get('max_test_seconds', 300))
        extra += ['-test-timeouts-enabled', 'YES', '-default-test-execution-time-allowance', str(allowance),
                  '-maximum-test-execution-time-allowance', str(allowance * 2)]
        bundles = self.run_dir / 'xcresults'
        bundles.mkdir(parents=True, exist_ok=True)
        bundle = bundles / f'test-{int(time.time())}.xcresult'
        extra += ['-resultBundlePath', str(bundle)]
        notes = []
        try:
            self.reset_simulator()
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            return {'exit_code': 75, 'cleanup_verified': False, 'output': f'Simulator reset failed: {error}'}
        notes.append(self.helper('helper_start'))
        try:
            code, output, reason = self.run(self.xcodebuild('test', extra), cancelled,
                                            int(self.config.get('run_cap_seconds', 3000)))
        finally:
            notes.append(self.helper('helper_stop'))
        cases = []
        if bundle.exists():
            parsed = subprocess.run(['xcrun', 'xcresulttool', 'get', 'test-results', 'tests', '--path', str(bundle)],
                                    capture_output=True, text=True, timeout=300)
            if parsed.returncode == 0:
                cases = parse_test_results(json.loads(parsed.stdout))
        counts, lines = summarize(cases)
        if code and not cases:
            # Nothing ran (build failure, xcodebuild error): never report that as OK.
            lines = [line for line in output.splitlines() if 'error:' in line][-15:]
            lines.append(f'No test results: xcodebuild exited {code} before any test ran')
        if reason:
            lines.append(f'Run stopped: {reason}')
        tail = '\n'.join(filter(None, notes)) + '\n' + output[-60000:] + '\n' + '\n'.join(lines)
        return {'exit_code': code, 'output': tail, 'cleanup_verified': True, 'stop_reason': reason,
                'instrumentation': {'counts': counts, 'tests': [
                    {'class': c['class'], 'test': c['test'], 'result': c['result'],
                     'code': 0 if c['result'].lower() in ('passed', 'skipped') else -2,
                     'messages': c['messages'][:3]} for c in cases]},
                'result_bundle': str(bundle)}
