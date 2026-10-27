"""Scoped OpenCode v1 HTTP/admission component; no launcher or daemon wiring.

HTTP acceptance, model acknowledgement and verified application are different
receipts. An uncertain submission is reconciled by stable ID, never retried.
"""

import base64
from dataclasses import asdict, dataclass
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .coordination import JournalConflict, encoded, required_text
from .supervisor import NoRedirect


class EngineError(RuntimeError):
    def __init__(self, category, status=None):
        self.category, self.status = category, status
        super().__init__('Engine request failed: ' + category)


def identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid engine identifier')
    return value


class EngineHTTP:
    def __init__(self, endpoint, username_env='ENGINE_USERNAME', password_env='ENGINE_PASSWORD',
                 timeout=5, expected_version='1.19.3'):
        parsed = urlsplit(endpoint)
        local = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if (not parsed.hostname or not (parsed.scheme == 'https' or local)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Engine endpoint requires HTTPS or loopback HTTP, without credentials/path')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 15:
            raise ValueError('Engine timeout must be between zero and fifteen seconds')
        self.endpoint, self.timeout = endpoint.rstrip('/'), timeout
        self.username_env, self.password_env = required_text(username_env), required_text(password_env)
        self.expected_version = required_text(expected_version)
        self.opener = build_opener(NoRedirect())
        self.last_next_cursor = None

    def request(self, method, path, payload=None, max_bytes=1024 * 1024):
        self.last_next_cursor = None
        if method not in ('GET', 'POST') or not path.startswith('/') or path.startswith('//'):
            raise ValueError('Invalid engine method/path')
        username, password = os.environ.get(self.username_env), os.environ.get(self.password_env)
        if (not username or not password or ':' in username
                or any(c in username + password for c in ('\r', '\n', '\x00'))):
            raise EngineError('configuration')
        credential = base64.b64encode((username + ':' + password).encode()).decode()
        request = Request(self.endpoint + path,
            data=encoded(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Basic ' + credential, 'Content-Type': 'application/json'}, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                cursor=response.headers.get('X-Next-Cursor')
                if cursor is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,2048}',cursor):
                    raise EngineError('invalid_history_cursor')
                self.last_next_cursor=cursor
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise EngineError('oversized_response')
                return response.status, json.loads(body) if body else None
        except HTTPError as error:
            status = error.code
            error.close()
            category = 'configuration' if status in (301, 302, 303, 307, 308, 401, 403) else 'http_error'
            raise EngineError(category, status) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise EngineError('unavailable') from None
        except (ValueError, UnicodeError):
            raise EngineError('invalid_response') from None


@dataclass(frozen=True)
class Binding:
    task_id: str
    session_id: str
    directory: str
    agent: str
    provider: str
    model: str

    def __post_init__(self):
        identifier(self.session_id, 'ses_')
        for value in (self.task_id, self.agent, self.provider, self.model):
            required_text(value)
        directory = Path(self.directory)
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError('Engine workspace must be an existing absolute directory')
        object.__setattr__(self, 'directory', str(directory.resolve()))


class SelectedSession:
    """A pre-created, explicitly selected session with a verified process hook.

    Session creation and process ownership remain the launcher's responsibility.
    The authorization callback must authenticate the canonical Board author/task;
    message text is never allowed to choose a different task/session/workspace.
    """
    def __init__(self, journal, client, binding, selected_task_ids, execution_is_active, authorize):
        if (binding.task_id not in frozenset(selected_task_ids)
                or not callable(execution_is_active) or not callable(authorize)):
            raise ValueError('Engine requires selected scope, process verification and author authorization')
        self.journal, self.client, self.binding = journal, client, binding
        self.active, self.authorize = execution_is_active, authorize
        self.db = journal.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS engine_bindings (task_id TEXT PRIMARY KEY, binding TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS engine_controls (id TEXT PRIMARY KEY, binding TEXT NOT NULL,
                intention TEXT NOT NULL, state TEXT NOT NULL);
        ''')
        text = encoded(asdict(binding))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO engine_bindings VALUES (?,?)', (binding.task_id, text))
            if self.db.execute('SELECT binding FROM engine_bindings WHERE task_id=?',
                               (binding.task_id,)).fetchone()[0] != text:
                raise JournalConflict('Existing task has a different engine binding')

    def _path(self, suffix=''):
        return '/session/' + self.binding.session_id + suffix + '?' + urlencode({'directory': self.binding.directory})

    def _verify(self):
        if self.active() is not True:
            raise EngineError('execution_not_verified')

        status, health = self.client.request('GET', '/global/health')
        if (status != 200 or not isinstance(health, dict) or health.get('healthy') is not True
                or health.get('version') != self.client.expected_version):
            raise EngineError('version_or_health_mismatch')
        status, session = self.client.request('GET', self._path())
        if (status != 200 or not isinstance(session, dict) or session.get('id') != self.binding.session_id
                or session.get('directory') != self.binding.directory
                or session.get('version') != self.client.expected_version):
            raise EngineError('binding_mismatch')
        if self.active() is not True:
            raise EngineError('execution_not_verified')

    @staticmethod
    def delivery_text(body, request_id):
        return ('Manager guidance within the current authorized task:\n' + body
                + '\n\nAcknowledge receipt with this exact line: GUIDANCE_ACK ' + request_id
                + '\nThis acknowledgement records receipt, not verified application.')

    def deliver(self, message_id):
        row = self.journal._message(message_id)
        if row['stage'] != 'ready':
            raise JournalConflict('Existing delivery must be reconciled, never submitted again')
        if self.authorize(row['envelope'], row['body'], self.binding.task_id) is not True:
            raise EngineError('unauthorized_guidance')
        self._verify()
        request_id = 'msg_' + hashlib.sha256(encoded([
            self.journal.identity.session_id, self.binding.session_id, message_id]).encode()).hexdigest()[:32]
        self.journal.prepare_delivery(message_id, self.binding.session_id, request_id)
        # Any exception after preparing this intention leaves a durable uncertain
        # outcome. Neither a timeout nor a missing read permits another POST.
        if self.active() is not True:
            raise EngineError('execution_not_verified')
        status, _ = self.client.request('POST', self._path('/prompt_async'), {
            'messageID': request_id, 'agent': self.binding.agent,
            'model': {'providerID': self.binding.provider, 'modelID': self.binding.model},
            'parts': [{'type': 'text', 'text': self.delivery_text(row['body'], request_id)}]})
        if status != 204:
            raise EngineError('unexpected_admission_status', status)
        evidence = {'engine_session': self.binding.session_id, 'request_id': request_id, 'observation': 'http_204'}
        self.journal.record_receipt(message_id, 'submitted', evidence)
        return evidence

    def message(self, request_id):
        identifier(request_id, 'msg_')
        self._verify()
        # One assistant message can embed full build/emulator logs: same bounded
        # 16 MiB ceiling as history pages, not the 1 MiB control-request limit.
        status, value = self.client.request('GET', self._path('/message/' + request_id), max_bytes=16 * 1024 * 1024)
        if (status != 200 or not isinstance(value, dict) or not isinstance(value.get('info'), dict)
                or value['info'].get('id') != request_id
                or value['info'].get('sessionID') != self.binding.session_id
                or not isinstance(value.get('parts'), list)
                or any(not isinstance(part, dict) for part in value['parts'])):
            raise EngineError('message_mismatch')
        return value

    def reconcile_delivery(self, message_id):
        row = self.journal._message(message_id)
        if row['engine_session'] != self.binding.session_id or not row['request_id']:
            raise JournalConflict('Message does not belong to this engine session')
        value = self.message(row['request_id'])
        text = [part.get('text') for part in value['parts'] if part.get('type') == 'text']
        if (value['info'].get('role') != 'user'
                or text != [self.delivery_text(row['body'], row['request_id'])]):
            raise EngineError('message_mismatch')
        if row['stage'] == 'prepared':
            self.journal.record_receipt(message_id, 'submitted', {
                'engine_session': self.binding.session_id, 'request_id': row['request_id'],
                'observation': 'canonical_user_message'})
        return {'observed': True, 'acknowledged': row['stage'] in ('acknowledged', 'applied'),
                'applied': row['stage'] == 'applied'}

    def acknowledge(self, message_id, assistant_id):
        row = self.journal._message(message_id)
        if row['engine_session'] != self.binding.session_id or row['stage'] not in ('submitted', 'acknowledged'):
            raise JournalConflict('Admission evidence is required before acknowledgement')
        value = self.message(assistant_id)
        marker = 'GUIDANCE_ACK ' + row['request_id']
        if (value['info'].get('role') != 'assistant' or value['info'].get('parentID') != row['request_id']
                or not any(part.get('type') == 'text' and isinstance(part.get('text'), str)
                           and re.search(r'^'+re.escape(marker)+r'$', part['text'], re.M)
                           for part in value['parts'])):
            raise EngineError('acknowledgement_not_observed')
        self.journal.record_receipt(message_id, 'acknowledged', {
            'engine_session': self.binding.session_id, 'assistant_id': assistant_id,
            'request_id': row['request_id'], 'observation': 'assistant_marker'})
        return {'acknowledged': True, 'applied': False}

    def notice(self, key, text):
        """Send one harness-authored instruction (not Board guidance) into the session.

        Recorded before the POST under a stable id: a notice is submitted at most once,
        and an uncertain outcome is never retried.
        """
        request_id = 'msg_' + hashlib.sha256(encoded([self.binding.session_id, 'notice', required_text(key)]).encode()).hexdigest()[:32]
        binding, intention = encoded(asdict(self.binding)), encoded({'notice': key})
        with self.db:
            if self.db.execute('SELECT 1 FROM engine_controls WHERE id=?', (request_id,)).fetchone():
                return {'state': 'already_sent', 'request_id': request_id}
            self.db.execute('INSERT INTO engine_controls VALUES (?,?,?,?)', (request_id, binding, intention, 'prepared'))
        self._verify()
        status, _ = self.client.request('POST', self._path('/prompt_async'), {
            'messageID': request_id, 'agent': self.binding.agent,
            'model': {'providerID': self.binding.provider, 'modelID': self.binding.model},
            'parts': [{'type': 'text', 'text': 'Harness notice (automatic, from the harness itself):\n' + required_text(text)}]})
        if status != 204:
            raise EngineError('unexpected_admission_status', status)
        with self.db:
            self.db.execute("UPDATE engine_controls SET state='accepted' WHERE id=?", (request_id,))
        return {'state': 'accepted', 'request_id': request_id}

    def abort(self, intention):
        if not isinstance(intention, dict):
            raise ValueError('A durable coordinator control intention is required')
        request_id = required_text(intention.get('id'))
        required_text(intention.get('reason'))
        binding, text = encoded(asdict(self.binding)), encoded(intention)
        with self.db:
            saved = self.db.execute('SELECT binding,intention,state FROM engine_controls WHERE id=?',
                                    (request_id,)).fetchone()
            if saved:
                if saved[:2] != (binding, text):
                    raise JournalConflict('Control identity changed')
                return {'state': saved[2], 'quiescent': False, 'cleanup_verified': False}
            self.db.execute('INSERT INTO engine_controls VALUES (?,?,?,?)',
                            (request_id, binding, text, 'prepared'))
        self._verify()
        status, result = self.client.request('POST', self._path('/abort'))
        if status != 200 or result is not True:
            raise EngineError('abort_outcome_uncertain', status)
        with self.db:
            self.db.execute("UPDATE engine_controls SET state='accepted' WHERE id=?", (request_id,))
        # An HTTP abort receipt does not prove tool/process quiescence or cleanup.
        return {'state': 'accepted', 'quiescent': False, 'cleanup_verified': False}


def command_results(message):
    """Expose raw tool evidence; completed tool status does not mean exit zero."""
    results = []
    if not isinstance(message, dict) or not isinstance(message.get('parts'), list):
        raise EngineError('invalid_response')
    for part in message['parts']:
        if not isinstance(part, dict):
            raise EngineError('invalid_response')
        if part.get('type') != 'tool' or part.get('tool') not in ('bash', 'shell'):
            continue
        state = part.get('state', {})
        if (not isinstance(state, dict) or not isinstance(state.get('metadata', {}), dict)
                or not isinstance(state.get('input', {}), dict)):
            raise EngineError('invalid_response')
        code = state.get('metadata', {}).get('exit')
        if type(code) is not int:
            code = None
        results.append({'call_id': part.get('callID'), 'status': state.get('status'), 'exit_code': code,
                        'passed': state.get('status') == 'completed' and code == 0,
                        'command': state.get('input', {}).get('command'),
                        'output': state.get('output'), 'error': state.get('error'),
                        'truncated': state.get('metadata', {}).get('truncated')})
    return results
