from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from strata_board_harness.board import BoardError
from strata_board_harness.runtime import Runtime, private_json
from strata_board_harness.supervisor import Store
from test_coordinator import FakeBoard, IDENTITY, message


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.addCleanup(self.store.close)
        self.now = 0
        self.board = FakeBoard(lambda: self.now)
        self.registers = 0
        def register():
            self.registers += 1
            if getattr(self, 'fail_register', False): raise BoardError('unavailable')
        self.board.register = register
        self.board_file = self.root / 'board.json'
        self.save(self.board_file, {'identity': asdict(IDENTITY), 'profile_version': 1})
        self.config = self.root / 'runtime.json'
        self.save(self.config, {'board_file': str(self.board_file), 'control_thread_ids': ['selected-thread']})
        self.runtime = Runtime(self.store, self.config, lambda *args: self.board, clock=lambda: self.now)
        self.addCleanup(self.runtime.close)

    def save(self, path, data):
        path.write_text(json.dumps(data));path.chmod(0o600)

    def test_idle_daemon_polls_and_persists_guidance_while_model_offline_without_claim(self):
        self.board.inbox = [message('ping', 'hello')]
        self.board.bodies['ping'] = 'hello'
        for self.now in range(0, 100, 5): self.runtime.tick('waiting_for_model')
        self.assertEqual(self.registers, 1)
        self.assertEqual(self.board.mutations, [])
        self.assertEqual(self.store.get('runtime')['state'], 'waiting_for_model')
        self.assertEqual(self.runtime.journal.pending()[0]['body'], 'hello')
        cursor = self.runtime.journal.cursor()
        self.runtime.close()
        runtime = Runtime(self.store, self.config, lambda *args: self.board, clock=lambda: self.now)
        self.addCleanup(runtime.close)
        runtime.tick('waiting_for_model')
        self.assertEqual(len(runtime.journal.pending()), 1)
        self.assertIsNone(runtime.coordinator.state['stop_reason'])
        self.assertEqual(self.store.get('runtime')['state'], 'waiting_for_model')
        self.assertTrue(cursor)
        self.assertEqual(self.board.mutations, [])

    def test_board_without_response_is_retried_briefly_before_stopping_the_execution(self):
        self.runtime.tick('waiting_for_model')
        original=self.runtime.worker.tick;failing={'error':BoardError('unavailable')}
        def tick(*args):
            if failing['error']: raise failing['error']
            return original(*args)
        self.runtime.worker.tick=tick
        for self.now in (10,60,150):
            self.runtime.tick('waiting_for_model')
            self.assertEqual(self.store.get('runtime')['state'],'board_unavailable_retrying')
            self.assertIsNone(self.runtime.coordinator.state['stop_reason'])
        failing['error']=None;self.now=160;self.runtime.tick('waiting_for_model')
        self.assertIsNone(self.runtime.board_unavailable_since)
        failing['error']=BoardError('unavailable')
        for self.now in (200,300,381):self.runtime.tick('waiting_for_model')
        self.assertEqual(self.runtime.coordinator.state['stop_reason'],'runtime_execution_error')

    def test_board_rejection_still_stops_the_execution_immediately(self):
        self.runtime.tick('waiting_for_model')
        def tick(*args): raise BoardError('forbidden',403)
        self.runtime.worker.tick=tick;self.now=10;self.runtime.tick('waiting_for_model')
        self.assertEqual(self.runtime.coordinator.state['stop_reason'],'runtime_execution_error')

    def test_registration_retries_have_bounded_backoff_and_no_worker_start(self):
        self.fail_register = True
        for self.now in range(50): self.runtime.tick('model_available')
        self.assertEqual(self.registers, 4)
        self.assertEqual(self.store.get('runtime')['state'], 'connection_requires_attention')
        self.fail_register = False
        self.now = 76
        self.runtime.tick('model_available')
        self.assertEqual(self.store.get('runtime')['state'], 'idle')
        self.assertEqual(self.board.mutations, [])

    def test_missing_pairing_does_not_kill_daemon_and_profile_drift_is_rejected(self):
        self.board_file.unlink()
        self.runtime.tick('model_available')
        self.assertEqual(self.store.get('runtime')['state'], 'waiting_for_pairing_or_configuration')
        self.save(self.board_file, {'identity': asdict(IDENTITY), 'profile_version': 2})
        self.now = 10;self.runtime.tick('model_available')
        self.assertEqual(self.store.get('runtime')['error_type'], 'paired_profile_version_changed')
        self.assertIsNone(self.runtime.coordinator)

    def test_selected_task_without_execution_adapter_cannot_claim(self):
        self.save(self.config, {'board_file': str(self.board_file), 'selected_task': {
            'ticket_id': 'selected-task', 'thread_id': 'selected-thread', 'authorization_id': 'human-pilot'}})
        self.runtime.tick('model_available')
        self.assertEqual(self.store.get('runtime')['state'], 'connection_requires_attention')
        self.assertEqual(self.board.mutations, [])

    def test_private_credentials_reject_symlink_or_group_readable_file(self):
        self.board_file.chmod(0o640)
        with self.assertRaises(ValueError): private_json(self.board_file)
        link = self.root / 'link';link.symlink_to(self.config)
        with self.assertRaises(ValueError): private_json(link)


if __name__ == '__main__': unittest.main()
