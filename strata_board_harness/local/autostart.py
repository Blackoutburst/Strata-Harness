"""Start `harness serve` at login: launchd (macOS), systemd user unit (Linux), Task Scheduler (Windows)."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from .agents import harness_home

REPO = Path(__file__).resolve().parents[2]
MAC = sys.platform == 'darwin'
WINDOWS = os.name == 'nt'
NAME = 'dev.strata-harness' if MAC else 'strata-harness'


def command():
    python = sys.executable
    if WINDOWS and Path(python).with_name('pythonw.exe').exists():
        python = str(Path(python).with_name('pythonw.exe'))
    return [python, '-u', '-m', 'strata_board_harness.local', 'serve']


def install():
    home = harness_home()
    home.mkdir(parents=True, exist_ok=True)
    log = home / 'serve.log'
    if MAC:
        path = Path.home() / 'Library' / 'LaunchAgents' / f'{NAME}.plist'
        path.parent.mkdir(parents=True, exist_ok=True)
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        if os.environ.get('HARNESS_HOME'):
            environment['HARNESS_HOME'] = os.environ['HARNESS_HOME']
        with open(path, 'wb') as stream:
            plistlib.dump({'Label': NAME, 'ProgramArguments': command(), 'WorkingDirectory': str(REPO),
                           'EnvironmentVariables': environment, 'RunAtLoad': True,
                           'KeepAlive': {'SuccessfulExit': False},
                           'StandardOutPath': str(log), 'StandardErrorPath': str(log)}, stream)
        domain = f'gui/{os.getuid()}'
        subprocess.run(['launchctl', 'bootout', domain, str(path)], capture_output=True)
        subprocess.check_call(['launchctl', 'bootstrap', domain, str(path)])
        return f'launchd agent {path}'
    if WINDOWS:
        line = subprocess.list2cmdline(command())
        subprocess.check_call(['schtasks', '/Create', '/F', '/SC', 'ONLOGON', '/RL', 'LIMITED', '/TN', NAME,
                               '/TR', f'cmd /c cd /d "{REPO}" && {line} >> "{log}" 2>&1'])
        subprocess.check_call(['schtasks', '/Run', '/TN', NAME])
        return f'scheduled task {NAME}'
    if not shutil.which('systemctl'):
        raise SystemExit('systemctl not found; start `harness serve` from your session startup instead.')
    path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd' / 'user' / f'{NAME}.service'
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted = ' '.join('"' + part.replace('"', '\\"') + '"' for part in command())
    extra = f'Environment="HARNESS_HOME={os.environ["HARNESS_HOME"]}"\n' if os.environ.get('HARNESS_HOME') else ''
    path.write_text(f'''[Unit]
Description=Strata harness agents
After=network-online.target

[Service]
ExecStart={quoted}
WorkingDirectory={REPO}
Environment="PATH={os.environ.get("PATH", "/usr/bin:/bin")}"
{extra}Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
''')
    subprocess.check_call(['systemctl', '--user', 'daemon-reload'])
    subprocess.check_call(['systemctl', '--user', 'enable', '--now', NAME])
    return f'systemd user unit {path} (run `loginctl enable-linger $USER` to start before login)'


def uninstall():
    if MAC:
        path = Path.home() / 'Library' / 'LaunchAgents' / f'{NAME}.plist'
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    elif WINDOWS:
        subprocess.run(['schtasks', '/End', '/TN', NAME], capture_output=True)
        subprocess.run(['schtasks', '/Delete', '/F', '/TN', NAME], capture_output=True)
    else:
        subprocess.run(['systemctl', '--user', 'disable', '--now', NAME], capture_output=True)
        path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd' / 'user' / f'{NAME}.service'
        path.unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
