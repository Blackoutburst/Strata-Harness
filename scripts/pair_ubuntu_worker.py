#!/usr/bin/env python3
"""Pair a NEW Ubuntu worker using the owner's normal interactive Board login.

Run on the Mac that owns the local Board. No manager credential is copied.
Passwords/codes/tokens are never printed or stored in this source directory.
"""

import argparse
from datetime import datetime, timezone
import getpass
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.error import HTTPError
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strata_board_harness.board import BoardClient, MCPHTTP
from strata_board_harness.coordination import Identity
from strata_board_harness.supervisor import NoRedirect


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('w') as output:
        path.chmod(0o600)
        json.dump(data, output, indent=2)
        output.write('\n')


class OwnerHTTP:
    def __init__(self, base):
        parsed = urlsplit(base)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path not in ('', '/')
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Interactive pairing requires the local loopback Board')
        self.base, self.csrf = base.rstrip('/'), None
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, payload=None):
        headers = {'Content-Type': 'application/json', 'Origin': self.base}
        if self.csrf: headers['x-csrf-token'] = self.csrf
        try:
            with self.opener.open(Request(self.base + path, method=method, headers=headers,
                data=json.dumps(payload).encode() if payload is not None else None), timeout=15) as response:
                return json.loads(response.read(2 * 1024 * 1024))
        except HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError('Owner Board request failed with HTTP ' + str(status)) from None


REMOTE_INSTALL = """import json,os,sys
from pathlib import Path
path=Path.home()/'.config/strata-board-harness/board.json'
if path.exists():raise SystemExit('Existing worker configuration: reconcile instead of replacing')
data=json.load(sys.stdin)
path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as output:json.dump(data,output,indent=2);output.write('\\n')
print('Distinct worker configuration saved privately')
"""


def pair(args):
    if not args.host or args.host.startswith('-') or any(c.isspace() for c in args.host):
        raise ValueError('An explicit SSH host is required')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
    if args.ssh_control: ssh += ['-S', str(args.ssh_control)]
    check = subprocess.run(ssh + [args.host, "test ! -e ~/.config/strata-board-harness/board.json"],
                           capture_output=True, timeout=15)
    if check.returncode: raise RuntimeError('SSH/config preflight failed; no pairing created')
    state = Path.home() / '.local/state/strata-board-harness-pairing' / 'attempt.json'
    if state.exists():
        progress = json.loads(state.read_text())
        if (not getattr(args, 'resume_prepared', False) or progress.get('stage') != 'prepared'
                or progress.get('host') != args.host or progress.get('project_id') != args.project_id):
            raise RuntimeError('A prior pairing attempt exists; reconcile its receipt before retrying')
        operation = progress['operation_id']
    else:
        operation = uuid4().hex
        progress = {'operation_id': operation, 'started_at': datetime.now(timezone.utc).isoformat(),
                    'host': args.host, 'project_id': args.project_id, 'stage': 'prepared'}
        private_write(state, progress)
    owner = OwnerHTTP(args.board_url)
    try:
        password_file = getattr(args, 'password_file', None)
        if password_file:
            import stat
            password_path = Path(password_file)
            info = password_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Owner password file must be private and owned')
            password = password_path.read_text().strip()
        else:
            password = getpass.getpass('Local Board owner password (hidden): ')
        login = owner.call('POST', '/api/login', {'username': args.owner, 'password': password})
        password = None
        if login.get('actor', {}).get('owner_id') != args.owner: raise RuntimeError('Unexpected authenticated owner')
        owner.csrf = login['csrf_token']
        profile = owner.call('POST', '/api/agent-profiles', {
            'name': 'Strata Harness · Ubuntu', 'idempotency_key': 'strata-harness-' + operation,
            'role': 'Selected Phone E2E harness pilot developer', 'project_ids': [args.project_id],
            'preferred_provider': 'strata', 'preferred_model': 'swift-1.5-iq3_s',
            'instructions': 'Only manager-selected harness pilot work. Never inspect prior model implementations/results or take unrelated backlog. Stop at review with exact commit and validation receipts.',
            'policy': {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
                'allow_create_projects': False, 'autonomy': 'requested', 'actions': [], 'resources': []}})
        progress.update(profile_id=profile['id'], stage='profile_created');private_write(state, progress)
        code = owner.call('POST', '/api/settings/pairings', {'label': 'Strata Harness · Ubuntu home server',
                                                           'capabilities': ['python', 'android', 'e2e']})
        paired = owner.call('POST', '/mcp/connect/exchange', {'code': code['code']})
        if paired.get('owner_id') != args.owner or paired.get('mcp_url') != args.board_url.rstrip('/') + '/mcp':
            raise RuntimeError('Pairing identity/address mismatch')
        # Preserve the newly issued credential privately on partial failure. Never
        # exchange/create another device automatically after an uncertain reply.
        private_write(state.parent / 'new-worker.private.json', paired)
        key, session = secrets.token_hex(16), 'strata-harness-ubuntu-' + operation
        owner.call('POST', '/api/agent-profiles/' + profile['id'] + '/bindings', {
            'expected_version': profile['version'], 'device_id': paired['device_id'],
            'identity_key': key, 'agent_kind': 'other'})
        identity = Identity(args.owner, profile['id'], paired['device_id'], session)
        env = 'STRATA_PAIRING_FRESH_TOKEN'
        if env in os.environ: raise RuntimeError('Reserved pairing environment name already exists')
        os.environ[env] = paired['token']
        try:
            client = BoardClient(MCPHTTP(paired['mcp_url'], token_env=env), identity, key, [],
                                 label='Strata Harness · Ubuntu')
            client.register()
        finally:
            os.environ.pop(env, None)
        configuration = {'identity': {'owner_id': identity.owner_id, 'agent_id': identity.agent_id,
            'device_id': identity.device_id, 'session_id': identity.session_id}, 'identity_key': key,
            'token': paired['token'], 'endpoint': args.remote_board_url, 'origin_endpoint': paired['mcp_url'],
            'label': 'Strata Harness · Ubuntu', 'profile_version': client.profile_version}
        import shlex
        result = subprocess.run(ssh + [args.host, 'python3 -c ' + shlex.quote(REMOTE_INSTALL)],
            input=json.dumps(configuration), text=True, capture_output=True, timeout=20)
        if result.returncode: raise RuntimeError('Remote private configuration write failed; new identity receipt preserved')
        progress.update(stage='paired_and_installed', agent_id=identity.agent_id, device_id=identity.device_id,
                        session_id=identity.session_id, profile_version=client.profile_version)
        private_write(state, progress)
        print('Ubuntu worker paired under a distinct identity. No task claimed or worker launched.')
        print('Receipt: ' + str(state))
    finally:
        if owner.csrf:
            try: owner.call('POST', '/api/logout')
            except Exception: print('Owner-session logout could not be confirmed; session expires normally.', file=sys.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--board-url', default='http://127.0.0.1:9140')
    parser.add_argument('--remote-board-url', default='http://127.0.0.1:19140/mcp')
    parser.add_argument('--ssh-control', type=Path)
    parser.add_argument('--password-file', type=Path, help='Existing private owner credential; never printed or exported')
    parser.add_argument('--resume-prepared', action='store_true', help='Resume only a verified pre-login setup receipt')
    try: pair(parser.parse_args())
    except Exception as error:
        print('Pairing stopped (' + type(error).__name__ + '); private receipts preserved. No automatic retry.', file=sys.stderr)
        raise SystemExit(1)
