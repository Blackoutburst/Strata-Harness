from dataclasses import asdict, replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from strata_board_harness.coordination import Identity, Journal, JournalConflict
from strata_board_harness.engine import Binding, EngineError, EngineHTTP, SelectedSession, command_results
from strata_board_harness.supervisor import Store


IDENTITY = Identity('fixture-owner', 'fixture-worker', 'fixture-device', 'fixture-session')
BODY = 'Checkpoint the build failure and retain its complete error.'


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.store = Store(self.directory / 'state')
        self.addCleanup(lambda: self.store.close())
        self.journal = Journal(self.store, IDENTITY)
        self.binding = Binding('selected-task', 'ses_owned', str(self.directory), 'fixture-agent', 'fixture', 'fixture')
        self.alive, self.authorized = True, True
        self.version, self.session_directory = '1.19.3', str(self.directory)
        self.requests, self.messages = [], {}
        self.drop_prompt = self.drop_abort = False
        self.http_status, self.oversized = 200, False
        self.next_cursor = None
        self.after_session_read = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                path = urlsplit(self.path)
                size = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(size)) if size else None
                owner.requests.append((self.command, path.path, parse_qs(path.query), body))
                if owner.http_status != 200:
                    self.send_response(owner.http_status)
                    if owner.http_status == 302: self.send_header('Location', '/credential-trap')
                    self.end_headers()
                    return
                status, result = 200, None
                if path.path == '/global/health':
                    result = {'healthy': True, 'version': owner.version}
                elif path.path == '/session/ses_owned':
                    result = {'id': 'ses_owned', 'directory': owner.session_directory, 'version': owner.version}
                    if owner.after_session_read: owner.after_session_read()
                elif path.path.endswith('/prompt_async'):
                    mid = body['messageID']
                    owner.messages[mid] = {'info': {'id': mid, 'sessionID': 'ses_owned', 'role': 'user'}, 'parts': body['parts']}
                    status = 204
                    if owner.drop_prompt:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                elif '/message/' in path.path:
                    result = owner.messages.get(path.path.rsplit('/', 1)[1])
                    if result is None: status = 404
                elif path.path.endswith('/abort'):
                    result = True
                    if owner.drop_abort:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if owner.next_cursor is not None:self.send_header('X-Next-Cursor',owner.next_cursor)
                self.end_headers()
                if status != 204:
                    data = b'x' * (1024 * 1024 + 1) if owner.oversized else json.dumps(result).encode()
                    try: self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError): pass
            do_GET = do_POST = handle_request
            def log_message(self, *_): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        environment = patch.dict(os.environ, {'FIXTURE_ENGINE_USERNAME': 'fixture', 'FIXTURE_ENGINE_PASSWORD': 'fixture-private'})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = EngineHTTP(f'http://127.0.0.1:{self.server.server_port}',
                                 'FIXTURE_ENGINE_USERNAME', 'FIXTURE_ENGINE_PASSWORD')
        self.engine = self.make_engine()
        session = asdict(IDENTITY)
        session['id'] = session.pop('session_id')
        self.journal.persist_page(None, {'session': session, 'cursor': 'cursor-1', 'messages': [
            {'id': 'ping-1', 'author_id': 'trusted-manager', 'thread_id': 'selected-thread'}]})
        self.journal.save_body('ping-1', BODY, hashlib.sha256(BODY.encode()).hexdigest())

    def test_large_assistant_message_with_embedded_logs_is_readable(self):
        big = {'info': {'id': 'msg_big', 'sessionID': 'ses_owned', 'role': 'assistant'},
               'parts': [{'type': 'text', 'text': 'x' * (2 * 1024 * 1024)}]}
        self.messages['msg_big'] = big
        session = self.make_engine()
        self.assertEqual(session.message('msg_big')['info']['id'], 'msg_big')
        self.assertEqual(self.client.request('GET', '/global/health')[0], 200)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def make_engine(self):
        return SelectedSession(self.journal, self.client, self.binding, ['selected-task'],
            execution_is_active=lambda: self.alive,
            authorize=lambda message, body, task: self.authorized and message['author_id'] == 'trusted-manager'
                      and message['thread_id'] == 'selected-thread' and task == 'selected-task')

    def restart(self):
        self.store.close()
        self.store = Store(self.directory / 'state')
        self.journal = Journal(self.store, IDENTITY)
        self.engine = self.make_engine()

    def posts(self, suffix):
        return [row for row in self.requests if row[0] == 'POST' and row[1].endswith(suffix)]

    def test_harness_notice_is_posted_once_and_survives_restart(self):
        first = self.engine.notice('evidence-abc', 'Quote the evidence first.')
        self.assertEqual(first['state'], 'accepted')
        text = self.posts('/prompt_async')[0][3]['parts'][0]['text']
        self.assertTrue(text.startswith('Harness notice'));self.assertIn('Quote the evidence first.', text)
        self.assertEqual(self.engine.notice('evidence-abc', 'Quote the evidence first.')['state'], 'already_sent')
        self.restart()
        self.assertEqual(self.engine.notice('evidence-abc', 'Quote the evidence first.')['state'], 'already_sent')
        self.assertEqual(len(self.posts('/prompt_async')), 1)

    def test_selected_delivery_is_not_acknowledgement_or_application(self):
        receipt = self.engine.deliver('ping-1')
        self.assertEqual(self.journal.pending()[0]['stage'], 'submitted')
        self.assertEqual(self.posts('/prompt_async')[0][3]['parts'], [{'type': 'text', 'text':
            self.engine.delivery_text(BODY, receipt['request_id'])}])
        for row in self.requests:
            if row[1].startswith('/session/'): self.assertEqual(row[2]['directory'], [str(self.directory)])
        observed = self.engine.reconcile_delivery('ping-1')
        self.assertEqual(observed, {'observed': True, 'acknowledged': False, 'applied': False})
        self.assertEqual(receipt['observation'], 'http_204')

    def test_lost_admission_reply_reconciles_after_restart_without_duplicate_post(self):
        self.drop_prompt = True
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        self.assertEqual(self.journal.pending()[0]['stage'], 'prepared')
        self.restart()
        with self.assertRaises(JournalConflict): self.engine.deliver('ping-1')
        self.assertTrue(self.engine.reconcile_delivery('ping-1')['observed'])
        self.assertEqual(self.journal.pending()[0]['stage'], 'submitted')
        self.assertEqual(len(self.posts('/prompt_async')), 1)

    def test_missing_or_changed_admission_never_authorizes_retry(self):
        self.drop_prompt = True
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        saved = dict(self.messages)
        self.messages.clear()
        with self.assertRaises(EngineError): self.engine.reconcile_delivery('ping-1')
        self.messages = saved
        next(iter(self.messages.values()))['parts'] = [{'type': 'text', 'text': 'Changed body'}]
        with self.assertRaises(EngineError): self.engine.reconcile_delivery('ping-1')
        with self.assertRaises(JournalConflict): self.engine.deliver('ping-1')
        self.assertEqual(len(self.posts('/prompt_async')), 1)

    def test_ack_requires_assistant_parent_and_exact_marker_and_never_implies_applied(self):
        self.engine.deliver('ping-1')
        request_id = self.journal.pending()[0]['request_id']
        reply = {'info': {'id': 'msg_ack', 'sessionID': 'ses_owned', 'role': 'user', 'parentID': request_id},
                 'parts': [{'type': 'text', 'text': 'GUIDANCE_ACK ' + request_id}]}
        self.messages['msg_ack'] = reply
        with self.assertRaises(EngineError): self.engine.acknowledge('ping-1', 'msg_ack')
        reply['info']['role'] = 'assistant';reply['info']['parentID'] = 'msg_other'
        with self.assertRaises(EngineError): self.engine.acknowledge('ping-1', 'msg_ack')
        reply['info']['parentID'] = request_id
        self.assertEqual(self.engine.acknowledge('ping-1', 'msg_ack'), {'acknowledged': True, 'applied': False})
        self.assertEqual(self.journal.pending()[0]['stage'], 'acknowledged')
        self.restart()
        self.assertEqual(self.journal.pending()[0]['stage'], 'acknowledged')

    def test_scope_identity_version_and_execution_checks_precede_mutation(self):
        self.authorized = False
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        self.assertEqual(self.requests, [])
        self.authorized = True;self.alive = False
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        self.alive = True;self.version = 'wrong-version'
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        self.version = '1.19.3';self.session_directory = '/wrong-workspace'
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        self.session_directory = str(self.directory);self.after_session_read = lambda: setattr(self, 'alive', False)
        with self.assertRaises(EngineError): self.engine.deliver('ping-1')
        self.assertEqual(self.posts('/prompt_async'), [])
        self.assertEqual(self.journal.pending()[0]['stage'], 'ready')

    def test_persisted_binding_cannot_change_on_restart(self):
        for binding in [replace(self.binding, session_id='ses_other'), replace(self.binding, model='other')]:
            with self.assertRaises(JournalConflict):
                SelectedSession(self.journal, self.client, binding, ['selected-task'], lambda: True, lambda *_: True)
        with self.assertRaises(ValueError):
            SelectedSession(self.journal, self.client, self.binding, ['other-task'], lambda: True, lambda *_: True)

    def test_accepted_or_uncertain_abort_is_not_repeated_or_reported_as_cleanup(self):
        intention = {'id': 'control-1', 'reason': 'manager_pause'}
        self.drop_abort = True
        with self.assertRaises(EngineError): self.engine.abort(intention)
        self.restart()
        self.assertEqual(self.engine.abort(intention), {'state': 'prepared', 'quiescent': False, 'cleanup_verified': False})
        self.assertEqual(len(self.posts('/abort')), 1)
        self.drop_abort = False
        second = {'id': 'control-2', 'reason': 'manager_stop'}
        self.assertEqual(self.engine.abort(second), {'state': 'accepted', 'quiescent': False, 'cleanup_verified': False})
        self.assertEqual(self.engine.abort(second)['state'], 'accepted')
        self.assertEqual(len(self.posts('/abort')), 2)

    def test_http_credentials_redirects_body_limits_and_paths_fail_closed(self):
        self.http_status = 302
        with self.assertRaises(EngineError) as result: self.client.request('GET', '/global/health')
        self.assertEqual(result.exception.category, 'configuration')
        self.assertNotIn('fixture-private', str(result.exception))
        self.assertFalse(any(row[1] == '/credential-trap' for row in self.requests))
        count = len(self.requests)
        with patch.dict(os.environ, {'FIXTURE_ENGINE_PASSWORD': ''}):
            with self.assertRaises(EngineError): self.client.request('GET', '/global/health')
        self.assertEqual(len(self.requests), count)
        self.http_status = 200;self.oversized = True
        with self.assertRaises(EngineError) as result: self.client.request('GET', '/global/health')
        self.assertEqual(result.exception.category, 'oversized_response')
        for endpoint in ['http://example.com', 'https://user:private@example.com', 'http://127.0.0.1/base']:
            with self.assertRaises(ValueError): EngineHTTP(endpoint)

    def test_completed_tool_with_nonzero_missing_or_false_exit_never_passes(self):
        for code, passed in [(37, False), (None, False), (False, False), (0, True)]:
            row = {'parts': [{'type': 'tool', 'tool': 'bash', 'callID': 'owned-call',
                'state': {'status': 'completed', 'input': {'command': 'owned build'},
                          'output': 'BUILD FAILED\ncomplete error', 'metadata': {'exit': code}}}]}
            result = command_results(row)[0]
            self.assertEqual(result['passed'], passed)
            self.assertEqual(result['output'], 'BUILD FAILED\ncomplete error')
        with self.assertRaises(EngineError): command_results({'parts': [None]})

    def test_opaque_cursor_header_is_bounded_and_cleared_between_responses(self):
        self.next_cursor='eyJpZCI6Im1zZ19hIiwidGltZSI6MX0'
        self.client.request('GET','/global/health')
        self.assertEqual(self.client.last_next_cursor,self.next_cursor)
        self.next_cursor=None;self.client.request('GET','/global/health')
        self.assertIsNone(self.client.last_next_cursor)
        self.next_cursor='http://unselected.invalid/credential-trap'
        with self.assertRaises(EngineError) as error:self.client.request('GET','/global/health')
        self.assertEqual(error.exception.category,'invalid_history_cursor')


if __name__ == '__main__':
    unittest.main()
