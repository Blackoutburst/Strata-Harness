"""One explicit fake-model engine preflight with a cross-host Mac reservation."""
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--host',required=True);parser.add_argument('--ssh-control',required=True)
    args=parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-S', args.ssh_control, args.host]
    lock = Path.home() / '.local/share/opencode-worker.lock'
    # The operator has selected the host; refuse every pre-existing reservation.
    lock.mkdir(mode=0o700)
    owner = {'nonce': secrets.token_hex(16), 'purpose': 'ubuntu_fake_model_engine_preflight', 'pid': os.getpid()}
    (lock / 'owner.json').write_text(json.dumps(owner));(lock / 'pid').write_text(str(os.getpid()))
    try:
        bundle = subprocess.run(['tar', '-czf', '-', '--exclude=__pycache__', '--exclude=*.pyc',
            'strata_board_harness', 'plugins', 'scripts'], cwd=root, capture_output=True, check=True)
        result = subprocess.run(ssh + ['tar -xzf - -C ~/.local/lib/strata-board-harness'], input=bundle.stdout,
                                capture_output=True, timeout=30)
        if result.returncode: raise RuntimeError('Source transfer failed')
        command = ('cd ~/.local/lib/strata-board-harness && '
            'PATH="$HOME/.local/share/strata-board-harness/tools:$PATH" python3 scripts/probe_opencode_control.py '
            '--binary "$HOME/.local/share/strata-board-harness/tools/opencode" --rtk-plugin plugins/rtk.ts '
            '--worker-lock "$HOME/.local/share/opencode-worker.lock" '
            '--output "$HOME/.local/share/strata-board-harness-setup/engine-control.json" '
            '--api-output "$HOME/.local/share/strata-board-harness-setup/openapi.json"')
        result = subprocess.run(ssh + [command], capture_output=True, text=True, timeout=240)
        (root / 'verification/2026-10-23-engine-ubuntu-control.txt').write_text(result.stdout + result.stderr)
        print('Ubuntu fake-model control exit:', result.returncode)
        receipt = subprocess.run(ssh + ['cat ~/.local/share/strata-board-harness-setup/engine-control.json'],
                                 capture_output=True, text=True, timeout=15)
        if receipt.returncode: raise RuntimeError('Engine receipt unavailable')
        data = json.loads(receipt.stdout)
        (root / 'verification/2026-10-23-engine-ubuntu-control.json').write_text(json.dumps(data, indent=2) + '\n')
        process = data.get('process_receipt', {})
        if not (process.get('server_exited') and process.get('temporary_files_removed')
                and process.get('owned_worker_lock_removed') and data.get('owned_tool_processes_exited')):
            raise RuntimeError('Owned process/resource cleanup is not proven; reservation retained')
        check = subprocess.run(ssh + ["ps -axo comm=; test ! -e ~/.local/share/opencode-worker.lock"],
                               capture_output=True, text=True, timeout=15)
        if check.returncode or any(Path(row.strip()).name == 'opencode' for row in check.stdout.splitlines()):
            raise RuntimeError('Remote worker may still exist; reservation retained')
        if json.loads((lock / 'owner.json').read_text()) != owner:
            raise RuntimeError('Reservation owner changed')
        (lock / 'owner.json').unlink();(lock / 'pid').unlink();lock.rmdir()
        print('Owned cross-host reservation released after remote process/lock cleanup checks')
        if result.returncode: raise SystemExit(result.returncode)
    except Exception:
        print('Preflight stopped; reservation retained for exact process/effect reconciliation')
        raise


if __name__ == '__main__': main()
