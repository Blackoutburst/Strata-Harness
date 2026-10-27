"""Install the idle user supervisor on its owning Ubuntu host.

Source is already copied by the operator. Existing private configuration is never
replaced. This script does not select tasks, start a coding engine, or pair.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess


def exclusive(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
        output.write(text)


def main():
    root = Path(__file__).resolve().parents[1]
    directory = Path.home() / '.config/strata-board-harness'
    runtime = directory / 'runtime.json'
    config = directory / 'config.json'
    environment = directory / 'environment'
    if not (directory / 'board.json').exists(): raise RuntimeError('Pair worker first')
    if not runtime.exists():
        exclusive(runtime, json.dumps({'board_file': str(directory / 'board.json'), 'control_thread_ids': []}, indent=2))
    if not config.exists():
        values = json.loads((root / 'config.example.json').read_text())
        values['runtime_file'] = str(runtime)
        exclusive(config, json.dumps(values, indent=2))
    if not environment.exists():
        key_file = Path.home() / '.config/strata/api-key'
        info = key_file.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Existing Strata credential must be private')
        key = key_file.read_text().strip()
        if not key or any(c in key for c in '\n\r\x00'): raise RuntimeError('Invalid private API credential')
        exclusive(environment, 'STRATA_API_KEY=' + json.dumps(key) + '\n')
    unit = Path.home() / '.config/systemd/user/strata-board-harness.service'
    unit.parent.mkdir(parents=True, exist_ok=True)
    if unit.exists(): raise RuntimeError('Existing service: inspect before upgrade')
    shutil.copyfile(root / 'deploy/strata-board-harness.user.service', unit)
    env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/' + str(os.getuid()))
    subprocess.run(['systemd-analyze', '--user', 'verify', str(unit)], env=env, check=True, timeout=15)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, check=True, timeout=15)
    subprocess.run(['systemctl', '--user', 'enable', '--now', unit.name], env=env, check=True, timeout=15)
    print('User supervisor installed; no task selected. Inspect status and Board identity next.')


if __name__ == '__main__': main()
