import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from strata_board_harness.coordination import Journal
from strata_board_harness.engine import EngineError
from strata_board_harness.managed_engine import ManagedEngine, group_members, process_stamp
from strata_board_harness.supervisor import Store
from test_coordinator import IDENTITY


class ManagedEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root / 'state');self.addCleanup(self.store.close)
        self.work = self.root / 'workspace';self.work.mkdir()
        self.tools = self.root / 'tools';self.tools.mkdir()
        for name, source in [('opencode-fixture', "printf 'http://127.0.0.1:19999\\n'\nsleep 60 &\nwait\n"),
                             ('rtk', "printf '{\"command\":\"rtk ls\"}\\n'\n")]:
            p = self.tools / name;p.write_text('#!/bin/sh\n' + source);p.chmod(0o700)
        self.journal = Journal(self.store, IDENTITY)
        self.guard = self.clean = True
        self.settings = {'engine_root': str(self.root / 'private'), 'workspace': str(self.work),
            'binary': str(self.tools / 'opencode-fixture'), 'tools_dir': str(self.tools),
            'ticket_id': 'selected-task', 'provider': 'fixture', 'model': 'fixture', 'step_budget': 10,
            'worker_lock': str(self.root / 'worker.lock'), 'opencode_config': {
                'agent': {'strata-harness': {'steps': 10}}, 'permission': {'*': 'deny'}}}
        self.engine = ManagedEngine(self.store, self.journal, self.settings,
            lambda: self.guard, lambda: self.clean, lambda *args: True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.clean = True
        self.engine.stop('test_cleanup')

    def test_launcher_has_private_environment_and_owned_group_cleanup(self):
        self.engine._launch()
        self.assertTrue(self.engine.process_active())
        self.assertTrue(process_stamp(self.engine.child.pid))
        self.assertIn(self.engine.child.pid, group_members(self.engine.child.pid))
        for _ in range(20):
            if self.engine.discover_endpoint(): break
            time.sleep(.05)
        self.assertEqual(self.engine.state['endpoint'], 'http://127.0.0.1:19999')
        self.assertEqual((self.root / 'private/api.private.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('STRATA_HARNESS_OWN_BOARD_TOKEN', (self.root / 'private/config/opencode/opencode.json').read_text())
        self.assertTrue(self.engine.stop('fixture_complete'))
        self.assertFalse((self.root / 'worker.lock').exists())
        self.assertFalse(self.engine.process_active())

    def test_guard_failure_does_not_start_or_clear_foreign_lock(self):
        self.guard = False
        with self.assertRaises(EngineError): self.engine.prepare()
        lock = self.root / 'worker.lock';lock.mkdir();(lock / 'foreign').write_text('keep')
        self.guard = True
        with self.assertRaises(FileExistsError): self.engine._launch()
        self.assertEqual((lock / 'foreign').read_text(), 'keep')

    def test_gateway_cleanup_is_required_before_unlock(self):
        self.engine._launch();self.clean = False
        self.assertFalse(self.engine.stop('fixture_stop'))
        self.assertTrue((self.root / 'worker.lock').exists())
        self.assertEqual(self.engine.state['phase'], 'reconciliation_required')
        self.clean = True
        self.assertTrue(self.engine.stop('fixture_cleanup_verified'))
        self.assertFalse((self.root / 'worker.lock').exists())

    def test_session_creation_lost_reply_is_reconciled_without_second_post(self):
        self.engine._launch()
        self.engine.state['endpoint'] = 'http://127.0.0.1:19999'
        self.engine.state['creation_intent'] = 'known-creation'
        calls = []
        work = str(self.work)
        class Client:
            expected_version = '1.19.3'
            def request(self, method, path, payload=None, max_bytes=None):
                calls.append((method, path))
                if path == '/global/health': return 200, {'healthy': True, 'version': '1.19.3'}
                return 200, [{'id': 'ses_fixture', 'title': 'known-creation', 'directory': work, 'version': '1.19.3'}]
        self.engine.client = Client()
        self.assertTrue(self.engine.prepare())
        self.assertFalse(any(method == 'POST' for method, _ in calls))
        self.assertEqual(self.engine.state['session_id'], 'ses_fixture')

    def test_uncertain_creation_missing_from_list_is_not_retried(self):
        self.engine._launch();self.engine.state['endpoint'] = 'http://127.0.0.1:19999'
        self.engine.state['creation_intent'] = 'unknown-creation'
        class Client:
            def request(self, method, path, payload=None, max_bytes=None):
                if method == 'POST': raise AssertionError('Duplicate creation')
                return (200, {'healthy': True, 'version': '1.19.3'}) if path == '/global/health' else (200, [])
        self.engine.client = Client()
        with self.assertRaises(EngineError) as error: self.engine.prepare()
        self.assertEqual(error.exception.category, 'session_creation_requires_reconciliation')

    def test_reattach_existing_session_preserves_steps_and_running_state_without_a_prompt(self):
        self.engine._launch();self.engine.state.update(endpoint='http://127.0.0.1:19999',
            session_id='ses_fixture',active_started_at=1000,steps=17)
        calls=[];work=str(self.work)
        class Client:
            expected_version='1.19.3'
            def request(self,method,path,payload=None,max_bytes=None):
                calls.append((method,path))
                if path=='/global/health':return 200,{'healthy':True,'version':'1.19.3'}
                if method=='GET' and path.startswith('/session/ses_fixture'):
                    return 200,{'id':'ses_fixture','directory':work,'version':'1.19.3'}
                if method=='POST' and '/abort?' in path:return 200,True
                raise AssertionError('No session creation/prompt on reattachment')
        self.engine.client=Client()
        self.assertTrue(self.engine.prepare())
        self.assertEqual(self.engine.state['phase'],'running')
        self.assertEqual(self.engine.state['steps'],17)
        self.assertEqual(self.engine.state['active_started_at'],1000)
        self.assertEqual(calls,[('GET','/global/health')])

    def test_new_launch_ignores_previous_listening_url_until_current_process_announces(self):
        (self.root/'private').mkdir()
        (self.root/'private/server.log').write_text('Old process http://127.0.0.1:18888\n')
        (self.tools/'opencode-fixture').write_text('#!/bin/sh\nsleep 0.8\nprintf "http://127.0.0.1:19999\\n"\nsleep 60 &\nwait\n')
        self.engine._launch()
        self.assertFalse(self.engine.discover_endpoint())
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if self.engine.discover_endpoint():break
            time.sleep(.05)
        self.assertEqual(self.engine.state['endpoint'],'http://127.0.0.1:19999')

    def test_history_uses_opaque_header_and_exact_full_page_can_end_without_cursor(self):
        from urllib.parse import urlsplit,parse_qs
        self.engine._launch();self.engine.state['session_id']='ses_fixture'
        cursor='eyJpZCI6Im1zZ18yMCIsInRpbWUiOjIwMH0';calls=[]
        def rows(start,end):
            return [{'info':{'id':'msg_'+str(i),'sessionID':'ses_fixture','role':'assistant'},'parts':[]} for i in range(start,end)]
        class Client:
            expected_version='1.19.3';last_next_cursor=None
            def request(self,method,path,payload=None,max_bytes=None):
                calls.append(path);query=parse_qs(urlsplit(path).query)
                if 'before' not in query:self.last_next_cursor=cursor;return 200,rows(20,40)
                self.last_next_cursor=None
                if query['before']!=[cursor]:raise AssertionError('Message ID substituted for opaque cursor')
                return 200,rows(0,20)
        self.engine.client=Client();self.engine._bind()
        observed=self.engine.observe_history()
        self.assertEqual(len(observed),40);self.assertEqual(self.engine.state['steps'],40)
        self.assertEqual(len(calls),2)
        self.engine.session=None  # cleanup proves process ownership, not this fake HTTP fixture


if __name__ == '__main__': unittest.main()
