"""Process cleanup of the opt-in inspection scripts, using a fake executable."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.inspect_opencode import inspect


FAKE_ENGINE = r'''
import json, os, signal, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
if '--version' in sys.argv:
    print('owned-fixture')
    raise SystemExit(0)
def stopped(*_):
    Path(os.environ['HARNESS_EXIT_RECEIPT']).write_text(json.dumps({
        'workspace_exists': Path.cwd().is_dir(),
        'config_exists': Path(os.environ['XDG_CONFIG_HOME']).is_dir(),
    }))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stopped)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.headers.get('Authorization'):
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        body = {'paths': {}} if self.path == '/doc' else {'healthy': True, 'version': 'owned-fixture'}
        self.wfile.write(json.dumps(body).encode())
    def log_message(self, *_): pass
server = HTTPServer(('127.0.0.1', 0), Handler)
print('opencode server listening on http://127.0.0.1:' + str(server.server_port), flush=True)
server.serve_forever()
'''


class EngineInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='harness-inspect-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / 'owned-fixture'
        self.binary.write_text('#!' + sys.executable + '\n' + FAKE_ENGINE)
        self.binary.chmod(0o700)
        self.output = self.root / 'inspection.json'
        self.lock = self.root / 'worker.lock'
        self.exit_receipt = self.root / 'exit.json'

    def configuration(self, root, work, env):
        env['HARNESS_EXIT_RECEIPT'] = str(self.exit_receipt)
        return {}

    def run_inspection(self, exercise=None):
        return inspect(str(self.binary), self.lock, self.output, self.root / 'api.json',
                       configuration=self.configuration, exercise=exercise)

    def assert_cleaned(self):
        receipt = json.loads(self.output.read_text())
        self.assertTrue(receipt['server_exited'])
        self.assertTrue(receipt['temporary_files_removed'])
        self.assertTrue(receipt['owned_worker_lock_removed'])
        self.assertEqual(json.loads(self.exit_receipt.read_text()),
                         {'workspace_exists': True, 'config_exists': True})

    def test_server_exits_before_its_workspace_is_removed(self):
        result = self.run_inspection(lambda *_: {'owned_fixture': True})
        self.assertEqual(result['exercise'], {'owned_fixture': True})
        self.assert_cleaned()

    def test_exercise_failure_preserves_error_and_cleans_owned_server(self):
        def fail(*_):
            raise RuntimeError('owned injected fixture failure')
        with self.assertRaisesRegex(RuntimeError, 'owned injected fixture failure'):
            self.run_inspection(fail)
        self.assert_cleaned()

    def test_existing_lock_is_never_removed(self):
        self.lock.mkdir()
        marker = self.lock / 'owner.json'
        marker.write_text('another owner')
        with self.assertRaises(FileExistsError):
            self.run_inspection()
        self.assertEqual(marker.read_text(), 'another owner')
        self.assertFalse(self.exit_receipt.exists())

    def test_changed_lock_ownership_is_never_cleared(self):
        def replace_owner(*_):
            (self.lock / 'owner.json').write_text(json.dumps({'owner': 'changed'}))
        result = self.run_inspection(replace_owner)
        self.assertTrue(result['server_exited'])
        self.assertTrue(result['temporary_files_removed'])
        self.assertFalse(result['owned_worker_lock_removed'])
        self.assertEqual(json.loads((self.lock / 'owner.json').read_text()), {'owner': 'changed'})


if __name__ == '__main__':
    unittest.main()
