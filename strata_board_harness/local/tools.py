"""`harness-tool` for local agents: fixed build/test operations, never a raw device.

The agent calls `harness-tool build` or `harness-tool test [denied|class:...]`. A per-run
gateway (the pilot's JobGateway) runs Gradle in the agent's checkout and, for `test`,
the full instrumented suite on the agent's own isolated emulator through the pilot's
EmulatorRuntime (snapshot before, restore after, role and permission fixtures).
Raw adb stays denied to the agent.
"""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from ..emulator import EmulatorRuntime
from ..gateway import JobGateway
from ..managed_engine import process_stamp
from .agents import harness_home

REPO = Path(__file__).resolve().parents[2]
GRADLE_TASKS = ['assembleDebug', 'assembleDebugAndroidTest', 'testDebugUnitTest']


def targets():
    """Per-app test fixtures (role, denied permissions), keyed by package; owned by the manager."""
    path = harness_home() / 'targets.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def application_id(workspace):
    for name in ('app/build.gradle.kts', 'app/build.gradle'):
        path = Path(workspace) / name
        if path.is_file():
            match = re.search(r'applicationId\s*=?\s*["\']([\w.]+)["\']', path.read_text())
            if match:
                return match.group(1)
    raise RuntimeError('No applicationId found in app/build.gradle(.kts)')


def emulator_process(avd):
    rows = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=5).stdout
    for row in rows.splitlines():
        pid, _, command = row.strip().partition(' ')
        if 'qemu-system' in command and f'-avd {avd} ' in command + ' ':
            return int(pid)
    raise RuntimeError(f'Emulator {avd} is not running')


class LocalTools:
    def __init__(self, agent, workspace, run_dir, env):
        self.agent, self.workspace, self.env = agent, Path(workspace), env
        self.config = agent.config.get('tools') or {}
        handlers = {'build': self.build}
        if self.config.get('emulator'):
            handlers['test'] = self.test
        self.gateway = JobGateway(Path(run_dir) / 'jobs', handlers)

    def start(self, bin_dir):
        url = self.gateway.start()
        Path(bin_dir).mkdir(parents=True, exist_ok=True)
        script = Path(bin_dir) / 'harness-tool'
        script.write_text(f'#!/bin/sh\nexec python3 "{REPO / "scripts" / "harness_tool.py"}" "$@"\n')
        script.chmod(0o755)
        return {'HARNESS_GATEWAY_URL': url, 'HARNESS_GATEWAY_TOKEN': self.gateway.token}

    def stop(self):
        self.gateway.stop()

    def gradle(self, tasks, cancelled, cap=900):
        log = []
        child = subprocess.Popen(['./gradlew', '--console=plain', *tasks], cwd=self.workspace, env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 start_new_session=(os.name != 'nt'))
        deadline, reason = time.monotonic() + cap, None
        import threading
        reader = threading.Thread(target=lambda: log.extend(child.stdout), daemon=True)
        reader.start()
        while child.poll() is None:
            if cancelled.wait(.2):
                reason = 'cancelled'
            elif time.monotonic() >= deadline:
                reason = 'timeout'
            if reason:
                if os.name != 'nt':
                    os.killpg(child.pid, signal.SIGTERM)
                else:
                    child.terminate()
                break
        code = child.wait(timeout=60)
        reader.join(timeout=5)
        exit_code = 124 if reason == 'timeout' else (130 if reason == 'cancelled' else code)
        return {'exit_code': exit_code, 'output': ''.join(log), 'cleanup_verified': True, 'stop_reason': reason}

    def build(self, argument, cancelled):
        return self.gradle(GRADLE_TASKS, cancelled)

    def test(self, argument, cancelled):
        built = self.gradle(['assembleDebug', 'assembleDebugAndroidTest'], cancelled)
        if built['exit_code']:
            built['output'] = 'Build failed before the test run.\n' + built['output']
            return built
        emulator = self.config['emulator']
        package = application_id(self.workspace)
        pid = emulator_process(emulator['avd'])
        runtime = EmulatorRuntime({
            'serial': emulator['serial'], 'avd': emulator['avd'], 'emulator_pid': pid,
            'emulator_stamp': process_stamp(pid), 'avd_home': emulator['avd_home'],
            'adb': emulator['adb'], 'aapt2': emulator['aapt2'],
            'target': {'package': package, **targets().get(package, {})}}, self.agent.dir / 'emulator')
        apks = self.workspace / 'app/build/outputs/apk'
        return runtime.test_apks(apks / 'debug/app-debug.apk', apks / 'androidTest/debug/app-debug-androidTest.apk',
                                 cancelled, argument or 'normal')
