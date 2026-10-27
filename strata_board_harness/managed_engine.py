"""Owned, checkpointed OpenCode process and bounded v1 session lifecycle.

No task selection or claim is implied by starting the API. A verified external
cross-host guard and restricted tool gateway must be supplied before launch.
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import time
from urllib.parse import urlencode

from .coordination import JournalConflict, encoded, required_text
from .engine import Binding, EngineError, EngineHTTP, SelectedSession, command_results


HISTORY_PAGE_BYTES = 16 * 1024 * 1024


def process_stamp(pid):
    # lstart is locale-formatted; a fixed C locale keeps stamps comparable between
    # processes started from different environments (launchd vs a French shell).
    result = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'lstart=,pgid=,args='],
                            capture_output=True, text=True, timeout=3,
                            env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
    return result.stdout.strip() if result.returncode == 0 else ''


def group_members(group):
    rows = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], capture_output=True, text=True,
                          check=True, timeout=3).stdout.splitlines()
    return [int(fields[0]) for row in rows if len(fields := row.split()) >= 3
            and fields[1] == str(group) and not fields[2].startswith('Z')]


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
    temp.replace(path)


class ManagedEngine:
    def __init__(self, store, journal, settings, guard, gateway_clean, authorize, clock=time.time):
        if not callable(guard) or not callable(gateway_clean) or not callable(authorize):
            raise ValueError('Verified exclusivity, cleanup and guidance boundaries required')
        self.store, self.journal, self.settings, self.clock = store, journal, settings, clock
        self.guard, self.gateway_clean, self.authorize = guard, gateway_clean, authorize
        self.root = Path(settings['engine_root']).expanduser().resolve()
        self.workspace = Path(settings['workspace']).expanduser().resolve(strict=True)
        if self.root == self.workspace or self.root.is_relative_to(self.workspace):
            raise ValueError('Engine private state must be outside coding workspace')
        self.binary = str(Path(settings['binary']).expanduser().resolve(strict=True))
        self.tools = str(Path(settings['tools_dir']).expanduser().resolve(strict=True))
        self.task = required_text(settings['ticket_id'])
        self.agent = 'strata-harness'
        self.child = self.session = self.client = None
        self.next_observe = 0
        self.state = store.get('managed_engine', {'phase': 'new', 'steps': 0, 'progress_at': clock(),
            'active_started_at': None, 'stop_reason': None, 'cleanup_verified': False})
        if store.get('engine_selection') not in (None, {'task': self.task, 'workspace': str(self.workspace)}):
            raise JournalConflict('Engine selection changed')
        self.store.save({'engine_selection': {'task': self.task, 'workspace': str(self.workspace)}})
        self.password_env = 'STRATA_HARNESS_ENGINE_PASSWORD'
        self.username_env = 'STRATA_HARNESS_ENGINE_USERNAME'
        self._init_schema()

    def _init_schema(self):
        self.journal.db.executescript('''
            CREATE TABLE IF NOT EXISTS engine_messages (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL, observed_at REAL NOT NULL);
        ''')
        self.journal.db.commit()

    def save(self):
        self.store.save({'managed_engine': self.state})

    def process_active(self):
        saved = self.state.get('process')
        return bool(saved and saved['stamp'] and process_stamp(saved['pid']) == saved['stamp']
                    and saved['pid'] in group_members(saved['pid']))

    def _authentication(self):
        from .runtime import private_json
        credentials = private_json(self.root / 'api.private.json')
        os.environ[self.username_env] = credentials['username']
        os.environ[self.password_env] = credentials['password']
        self.client = EngineHTTP(self.state['endpoint'], self.username_env, self.password_env, timeout=15)

    def prepare(self):
        if self.guard() is not True: raise EngineError('cross_host_guard_not_verified')
        if self.state['phase'] in ('stopped', 'failed', 'reconciliation_required'): return False
        if self.state['phase'] == 'new': self._launch()
        if not self.process_active():
            self.state.update(phase='reconciliation_required', stop_reason='engine_process_not_verified')
            self.save();return False
        if not self.state.get('endpoint') and not self.discover_endpoint(): return False
        if self.client is None: self._authentication()
        status, health = self.client.request('GET', '/global/health')
        if status != 200 or health != {'healthy': True, 'version': '1.19.3'}:
            raise EngineError('version_or_health_mismatch')
        if self.state.get('session_id'):
            self._bind()
            # A live API reattachment, or a manager-reconciled process restart,
            # retains the existing session and its consumed step/time budget.
            # Stopped/reconciliation states still return above without launch.
            self.state['phase']='running' if self.state.get('active_started_at') is not None else 'ready'
            self.save()
            return True
        if self.state.get('creation_intent'):
            # Session creation has no documented idempotency key. Reconcile its
            # nonce from canonical list; never issue another POST after a loss.
            status, sessions = self.client.request('GET', '/session?' + urlencode({
                'directory': str(self.workspace), 'limit': 100}))
            matches = [s for s in sessions if isinstance(s, dict)
                       and s.get('title') == self.state['creation_intent']
                       and s.get('directory') == str(self.workspace)] if status == 200 and isinstance(sessions, list) else []
            if len(matches) != 1: raise EngineError('session_creation_requires_reconciliation')
            value = matches[0]
        else:
            self.state['creation_intent'] = 'harness-' + secrets.token_hex(16)
            self.save()
            status, value = self.client.request('POST', '/session?' + urlencode({'directory': str(self.workspace)}),
                                                {'title': self.state['creation_intent']})
            if status != 200: raise EngineError('session_creation_requires_reconciliation')
        if not isinstance(value, dict) or value.get('directory') != str(self.workspace) or value.get('version') != '1.19.3':
            raise EngineError('binding_mismatch')
        self.state.update(session_id=value['id'], phase='ready')
        self.save();self._bind()
        return True

    def _bind(self):
        self.session = SelectedSession(self.journal, self.client, Binding(self.task, self.state['session_id'],
            str(self.workspace), self.agent, self.settings['provider'], self.settings['model']),
            [self.task], self.process_active, self.authorize)

    def _launch(self):
        rows = subprocess.run(['ps', '-axo', 'comm='], capture_output=True, text=True, check=True, timeout=3).stdout
        if any(Path(row.strip()).name == 'opencode' for row in rows.splitlines()):
            raise EngineError('another_opencode_exists')
        lock = Path(self.settings['worker_lock']).expanduser()
        lock.mkdir(mode=0o700)
        self.state['lock'] = {'path': str(lock), 'nonce': secrets.token_hex(16), 'supervisor_pid': os.getpid()}
        write_private(lock / 'owner.json', self.state['lock'])
        self.save()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            credentials = {'username': 'strata-harness', 'password': secrets.token_urlsafe(32)}
            write_private(self.root / 'api.private.json', credentials)
            env = {key: os.environ[key] for key in ('LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
            env.update(HOME=str(self.root / 'home'), PATH=self.tools + ':/usr/local/bin:/usr/bin:/bin',
                RTK_OPENCODE_AGENT=self.agent, OPENCODE_SERVER_USERNAME=credentials['username'],
                OPENCODE_SERVER_PASSWORD=credentials['password'], OPENCODE_DISABLE_AUTOUPDATE='1',
                OPENCODE_DISABLE_DEFAULT_PLUGINS='1', OPENCODE_DISABLE_PROJECT_CONFIG='1',
                OPENCODE_DISABLE_LSP_DOWNLOAD='1', OPENCODE_DISABLE_MODELS_FETCH='1',
                OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER='1')
            # Forward only explicitly scoped gateway variables, never Board/SSH credentials.
            for key in ('HARNESS_GATEWAY_URL', 'HARNESS_GATEWAY_TOKEN', 'STRATA_MODEL_PROXY_TOKEN'):
                if key in os.environ: env[key] = os.environ[key]
            if self.settings.get('allow_raw_strata_key') is True and 'STRATA_API_KEY' in os.environ:
                env['STRATA_API_KEY']=os.environ['STRATA_API_KEY']
            for name in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
                env['XDG_' + name + '_HOME'] = str(self.root / name.lower())
            Path(env['HOME']).mkdir(exist_ok=True)
            config = self.root / 'config/opencode'
            (config / 'plugins').mkdir(parents=True, exist_ok=True)
            import shutil
            plugin_root = Path(__file__).resolve().parents[1] / 'plugins'
            for name in ('rtk.ts', 'guard.ts'): shutil.copyfile(plugin_root / name, config / 'plugins' / name)
            home_config = Path(env['HOME']) / '.config'
            if not home_config.exists(): home_config.symlink_to(self.root / 'config', target_is_directory=True)
            settings = dict(self.settings['opencode_config'])
            if settings.get('mcp', {}) or settings.get('agent', {}).get(self.agent, {}).get('steps') != self.settings['step_budget']:
                raise ValueError('Engine config requires no direct MCP and an exact step budget')
            settings.update(autoupdate=False, share='disabled', mcp={})
            write_private(config / 'opencode.json', settings)
            hook = subprocess.run([self.tools + '/rtk', 'hook', 'opencode', '--agent', self.agent, 'ls'],
                env=env, cwd=self.workspace, capture_output=True, text=True, timeout=10)
            if hook.returncode or not json.loads(hook.stdout).get('command', '').startswith('rtk '):
                raise EngineError('rtk_rewrite_gate_failed')
            self.state['rtk_preflight'] = json.loads(hook.stdout)
            log_path = self.root / 'server.log'
            # An appended log may contain a previous process's listening URL.
            # It must never satisfy readiness for this launch before it listens.
            self.state['server_log_offset']=log_path.stat().st_size if log_path.exists() else 0
            self.state.pop('endpoint',None);self.client=None
            with log_path.open('a') as log:
                log_path.chmod(0o600)
                self.child = subprocess.Popen([self.binary, 'serve', '--hostname', '127.0.0.1', '--port', '0',
                    '--print-logs', '--log-level', 'ERROR'], cwd=self.workspace, env=env,
                    stdout=log, stderr=log, start_new_session=True)
            # exec/shebang startup can change argv immediately after Popen.
            # Wait for a stable identity before storing it; later changes fence.
            self.state['process'] = {'pid': self.child.pid, 'stamp': process_stamp(self.child.pid)}
            self.save()
            stamp, consecutive = '', 0
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.child.poll() is None:
                observed = process_stamp(self.child.pid)
                consecutive = consecutive + 1 if observed and observed == stamp else 0
                stamp = observed
                if consecutive >= 2: break
                time.sleep(.05)
            if not stamp or consecutive < 2:
                raise EngineError('unstable_startup_process')
            self.state.update(process={'pid': self.child.pid, 'stamp': stamp}, phase='starting', started_at=self.clock())
            self.save()
        except Exception:
            self.state.update(phase='failed', stop_reason='launch_failed')
            self.save()
            self.stop('launch_failed')
            raise

    def discover_endpoint(self):
        if not self.process_active(): raise EngineError('execution_not_verified')
        data=(self.root / 'server.log').read_bytes()
        offset=self.state.get('server_log_offset',0)
        if not 0<=offset<=len(data):raise EngineError('server_log_reconciliation_required')
        log = data[offset:][-65536:].decode(errors='replace')
        matches = re.findall(r'http://127\.0\.0\.1:\d+', log)
        if matches:
            self.state['endpoint'] = matches[-1];self.save();return True
        if self.clock() - self.state.get('started_at', self.clock()) > 60:
            raise EngineError('startup_deadline')
        return False

    def start_prompt(self, text):
        if self.state['phase'] != 'ready' or not self.session or self.guard() is not True:
            raise EngineError('worker_not_ready')
        request_id = 'msg_' + secrets.token_hex(16)
        self.state.update(initial_request={'id': request_id, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'state': 'prepared'}, active_started_at=self.clock(), progress_at=self.clock(), phase='running')
        self.save()
        self.session._verify()
        status, _ = self.client.request('POST', self.session._path('/prompt_async'), {
            'messageID': request_id, 'agent': self.agent,
            'model': {'providerID': self.settings['provider'], 'modelID': self.settings['model']},
            'parts': [{'type': 'text', 'text': text}]})
        if status != 204: raise EngineError('unexpected_admission_status', status)
        self.state['initial_request']['state'] = 'submitted';self.save()

    def observe_history(self):
        if not self.session or not self.process_active(): raise EngineError('execution_not_verified')
        # Tool outputs (full build/emulator logs) make messages large: smaller pages,
        # with a bounded 16 MiB ceiling per page instead of the 1 MiB control limit.
        suffix = self.session._path('/message') + '&limit=8'
        pages = []
        for _ in range(3):
            status, messages = self.client.request('GET', suffix, max_bytes=HISTORY_PAGE_BYTES)
            if status != 200 or not isinstance(messages, list): raise EngineError('invalid_history')
            pages.extend(messages)
            cursor=getattr(self.client,'last_next_cursor',None)
            if not cursor: break
            ids = [m.get('info', {}).get('id') for m in messages]
            if not all(isinstance(i, str) for i in ids): raise EngineError('invalid_history')
            if all(self.journal.db.execute('SELECT 1 FROM engine_messages WHERE id=?', (i,)).fetchone() for i in ids): break
            # v1 uses an opaque timestamp+ID cursor supplied in the response
            # header, not the oldest message ID. Never follow a Link URL to a
            # different origin or synthesize a cursor from message content.
            suffix = self.session._path('/message') + '&' + urlencode({'limit': 8, 'before': cursor})
        else:
            raise EngineError('history_backlog_requires_reconciliation')
        latest = []
        for message in pages:
            info = message.get('info', {})
            if info.get('sessionID') != self.state['session_id'] or not isinstance(info.get('id'), str):
                raise EngineError('history_binding_mismatch')
            body = encoded(message);digest = hashlib.sha256(body.encode()).hexdigest()
            previous = self.journal.db.execute('SELECT digest FROM engine_messages WHERE id=?', (info['id'],)).fetchone()
            if not previous or previous[0] != digest:
                with self.journal.db:
                    self.journal.db.execute('INSERT OR REPLACE INTO engine_messages VALUES (?,?,?,?)',
                        (info['id'], digest, body, self.clock()))
                latest.append(message)
                # The worker compares actual source/build receipts; a completed
                # edit or repeated streaming text alone never refreshes progress.
        saved = [json.loads(row[0]) for row in self.journal.db.execute('SELECT body FROM engine_messages')]
        self.state['steps'] = sum(m.get('info', {}).get('role') == 'assistant' for m in saved)
        self.state['token_totals'] = {name: sum(m.get('info', {}).get('tokens', {}).get(name, 0) or 0 for m in saved)
                                      for name in ('input', 'output', 'reasoning')}
        self.state['cache_tokens'] = {name: sum(m.get('info', {}).get('tokens', {}).get('cache', {}).get(name, 0) or 0
                                               for m in saved) for name in ('read', 'write')}
        self.state['api_errors'] = [m['info']['error'] for m in saved if m.get('info', {}).get('error')]
        self.save()
        return latest

    def stop(self, reason):
        self.state['stop_reason'] = reason;self.save()
        saved = self.state.get('process')
        if saved and (self.process_active() or (self.child is not None and self.child.pid == saved['pid']
                                                 and self.child.poll() is None)):
            if self.session:
                try: self.session.abort({'id': 'stop-' + self.state['lock']['nonce'], 'reason': reason})
                except (EngineError, JournalConflict): pass
            os.killpg(saved['pid'], signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and group_members(saved['pid']): time.sleep(.05)
            if group_members(saved['pid']): os.killpg(saved['pid'], signal.SIGKILL)
            if self.child:
                try: self.child.wait(timeout=5)
                except subprocess.TimeoutExpired: raise EngineError('process_stop_uncertain') from None
        # Parent may have exited while its tools still live. Do not signal a
        # group without the verified leader; preserve the lock for reconciliation.
        if self.child and self.child.poll() is not None:
            self.child.wait(timeout=1)
        clean = bool(not saved or (not process_stamp(saved['pid']) and not group_members(saved['pid'])))
        clean = clean and self.gateway_clean() is True
        self.state.update(phase='stopped' if clean else 'reconciliation_required', cleanup_verified=clean,
                          stopped_at=self.clock())
        if clean and self.state.get('lock'):
            lock = Path(self.state['lock']['path'])
            if lock.exists():
                if json.loads((lock / 'owner.json').read_text()) != self.state['lock']:
                    raise EngineError('lock_owner_changed')
                (lock / 'owner.json').unlink();lock.rmdir()
        self.save()
        for name in (self.username_env, self.password_env): os.environ.pop(name, None)
        return clean
