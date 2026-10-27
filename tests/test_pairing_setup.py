import json
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.pair_ubuntu_worker import OwnerHTTP, pair, private_write


class PairingSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.args = SimpleNamespace(host='fixture@host', ssh_control=None, owner='fixture-owner',
            project_id='selected-project', board_url='http://127.0.0.1:9140',
            remote_board_url='http://127.0.0.1:19140/mcp')

    def test_private_file_mode_and_local_owner_endpoint(self):
        path = self.home / 'private/fixture.json'
        private_write(path, {'fixture': True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for base in ['https://example.com', 'http://user:password@example.com', 'http://localhost:9140',
                     'http://user:password@127.0.0.1:9140']:
            with self.assertRaises(ValueError): OwnerHTTP(base)

    def test_existing_remote_config_refuses_pairing_before_owner_login(self):
        with patch('scripts.pair_ubuntu_worker.subprocess.run', return_value=SimpleNamespace(returncode=1)), \
             patch('scripts.pair_ubuntu_worker.OwnerHTTP') as owner:
            with self.assertRaises(RuntimeError): pair(self.args)
            owner.assert_not_called()

    def test_distinct_pairing_installs_only_new_worker_credential_and_logs_out(self):
        calls, exports = [], []
        class Owner:
            csrf = None
            def __init__(self, base): pass
            def call(self, method, path, payload=None):
                calls.append((method, path, payload))
                if path == '/api/login': return {'actor': {'owner_id': 'fixture-owner'}, 'csrf_token': 'fixture-csrf'}
                if path == '/api/agent-profiles': return {'id': 'fresh-worker', 'version': 1}
                if path == '/api/settings/pairings': return {'code': 'AAAA-BBBB-CCCC-DDDD'}
                if path == '/mcp/connect/exchange': return {'token': 'new-fixture-worker-token', 'owner_id': 'fixture-owner',
                    'device_id': 'fresh-device', 'mcp_url': 'http://127.0.0.1:9140/mcp'}
                return {'ok': True}
        class Client:
            def __init__(self, transport, identity, key, selected, label):
                self.identity, self.profile_version = identity, 2
                if identity.agent_id != 'fresh-worker' or identity.device_id != 'fresh-device':
                    raise AssertionError('A different identity was substituted')
            def register(self): pass
        def ssh(command, **kwargs):
            if 'input' in kwargs: exports.append(json.loads(kwargs['input']))
            return SimpleNamespace(returncode=0)
        with patch('scripts.pair_ubuntu_worker.Path.home', return_value=self.home), \
             patch('scripts.pair_ubuntu_worker.OwnerHTTP', Owner), \
             patch('scripts.pair_ubuntu_worker.BoardClient', Client), \
             patch('scripts.pair_ubuntu_worker.getpass.getpass', return_value='fixture-owner-password'), \
             patch('scripts.pair_ubuntu_worker.subprocess.run', side_effect=ssh), redirect_stdout(io.StringIO()):
            pair(self.args)
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]['token'], 'new-fixture-worker-token')
        self.assertNotIn('password', exports[0])
        self.assertEqual(exports[0]['identity']['agent_id'], 'fresh-worker')
        self.assertEqual(exports[0]['endpoint'], self.args.remote_board_url)
        self.assertEqual(calls[-1][1], '/api/logout')
        profile = next(body for _, path, body in calls if path == '/api/agent-profiles')
        self.assertEqual(profile['project_ids'], ['selected-project'])
        self.assertFalse(profile['policy']['allow_create_projects'])
        self.assertEqual(profile['policy']['autonomy'], 'requested')
        receipt = json.loads((self.home / '.local/state/strata-board-harness-pairing/attempt.json').read_text())
        self.assertEqual(receipt['stage'], 'paired_and_installed')
        self.assertNotIn('token', receipt)

    def test_incomplete_pairing_attempt_prevents_automatic_duplicate_identity(self):
        receipt = self.home / '.local/state/strata-board-harness-pairing/attempt.json'
        private_write(receipt, {'stage': 'profile_created'})
        with patch('scripts.pair_ubuntu_worker.Path.home', return_value=self.home), \
             patch('scripts.pair_ubuntu_worker.subprocess.run', return_value=SimpleNamespace(returncode=0)), \
             patch('scripts.pair_ubuntu_worker.OwnerHTTP') as owner:
            with self.assertRaises(RuntimeError): pair(self.args)
            owner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
