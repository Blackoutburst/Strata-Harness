import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from strata_board_harness.gateway import JobGateway


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.started = threading.Event();self.release = threading.Event()
        def build(argument, cancelled):
            self.started.set()
            while not self.release.wait(.01):
                if cancelled.is_set(): return {'exit_code': 130, 'output': 'cancelled', 'cleanup_verified': True}
            return {'exit_code': 37, 'output': 'BUILD FAILED\nfull compiler diagnostic', 'cleanup_verified': True}
        self.gateway = JobGateway(self.directory, {'build': build})
        self.url = self.gateway.start()
        self.addCleanup(self.gateway.stop)

    def call(self, payload, token=None):
        request = Request(self.url + '/operation', data=json.dumps(payload).encode(),
            headers={'Authorization':'Bearer ' + (token or self.gateway.token)})
        with urlopen(request, timeout=3) as response: return json.load(response)

    def wait(self, identifier):
        for _ in range(100):
            result = self.gateway.status(identifier)
            if result['state'] not in ('prepared','running'): return result
            time.sleep(.01)
        self.fail('Owned job did not finish')

    def test_real_failure_and_full_log_are_preserved_without_shell_submission(self):
        job = self.call({'operation':'build'})
        self.assertTrue(self.started.wait(1))
        activity=self.gateway.activity()
        self.assertTrue(activity['verified_thread_alive'])
        self.assertEqual(activity['deadline_at']-activity['started_at'],905)
        with self.assertRaises(RuntimeError): self.gateway.submit('build')
        self.release.set();result = self.wait(job['id'])
        self.assertEqual(result['exit_code'],37);self.assertFalse(result['passed'])
        self.assertIn('full compiler diagnostic',Path(result['log']).read_text())
        self.assertTrue(result['cleanup_verified']);self.assertTrue(self.gateway.clean())
        self.assertIsNone(self.gateway.activity())
        page=self.call({'operation':'logs','argument':job['id']+':0'})
        self.assertIn('full compiler diagnostic',page['text'])
        restored = JobGateway(self.directory, {'build':lambda *_: None})
        self.assertEqual(restored.status(job['id'])['exit_code'],37)

    def test_authentication_and_operation_scope_reject_arbitrary_commands(self):
        with self.assertRaises(HTTPError) as error: self.call({'operation':'build'},'wrong')
        self.assertEqual(error.exception.code,401);error.exception.close()
        for payload in ({'operation':'shell','argument':'adb devices'}, {'operation':'build','argv':['sh']},
                        {'operation':'build','argument':'emulator-5558'}):
            with self.assertRaises(HTTPError) as error: self.call(payload)
            self.assertEqual(error.exception.code,409);error.exception.close()
        self.assertFalse(self.started.is_set())

    def test_class_filter_accepts_any_attentive_package_but_rejects_injection(self):
        seen=[]
        gateway=JobGateway(self.directory/'tests',{'test':lambda argument,_:seen.append(argument) or
                                                     {'exit_code':0,'output':'ok','cleanup_verified':True}})
        job=gateway.submit('test','class:tech.primal.attentive_os.contacts.e2e.WidgetE2ETest')
        gateway.threads[job['id']].join(1)
        self.assertEqual(seen,['class:tech.primal.attentive_os.contacts.e2e.WidgetE2ETest'])
        for argument in ('class:com.other.Foo','class:tech.primal.attentive_os.contacts.e2e.X;rm',
                         'class:tech.primal.attentive_os..Foo'):
            with self.assertRaises(ValueError): gateway.submit('test',argument)

    def test_handler_job_identity_does_not_replace_gateway_receipt(self):
        directory=self.directory/'identity'
        gateway=JobGateway(directory,{'test':lambda *_:{'id':'job-remote','operation':'test','argument':None,
                                                       'exit_code':1,'output':'1 failed','cleanup_verified':True}})
        job=gateway.submit('test','class:tech.primal.attentive_os.contacts.e2e.WidgetE2ETest')
        gateway.threads[job['id']].join(1)
        saved=json.loads((directory/(job['id']+'.json')).read_text())
        self.assertEqual(saved['state'],'completed');self.assertEqual(saved['id'],job['id'])
        self.assertEqual(saved['handler_id'],'job-remote');self.assertFalse((directory/'job-remote.json').exists())
        self.assertEqual(JobGateway(directory,{'test':lambda *_:None}).status(job['id'])['exit_code'],1)

    def test_commit_without_a_message_is_rejected_before_any_job(self):
        gateway=JobGateway(self.directory/'commit',{'commit':lambda *_:{'exit_code':0,'output':'','cleanup_verified':True}})
        with self.assertRaises(ValueError):gateway.submit('commit')
        with self.assertRaises(ValueError):gateway.submit('commit','')
        with self.assertRaises(ValueError):gateway.submit('commit','--help')
        with self.assertRaises(ValueError):gateway.submit('commit','fix')
        self.assertEqual(gateway.status()['jobs'],[])
        job=gateway.submit('commit','Add_hosted_widget_tests');gateway.threads[job['id']].join(1)
        self.assertEqual(gateway.status(job['id'])['state'],'completed')

    def test_cancel_waits_for_owned_job_cleanup_and_does_not_count_as_pass(self):
        job = self.gateway.submit('build');self.assertTrue(self.started.wait(1))
        self.assertTrue(self.gateway.stop())
        result = self.gateway.status(job['id'])
        self.assertEqual(result['exit_code'],130);self.assertFalse(result['passed'])

    def test_restart_with_unreconciled_job_refuses_duplicate_execution(self):
        p=self.directory/'job-uncertain.json';p.write_text(json.dumps({'id':'job-uncertain','state':'running'}))
        with self.assertRaises(RuntimeError): JobGateway(self.directory,{'build':lambda *_:None})

    def test_handler_without_cleanup_or_exit_evidence_is_fenced(self):
        gateway=JobGateway(self.directory/'other',{'build':lambda *_:{'output':'claimed success'}})
        job=gateway.submit('build')
        for _ in range(100):
            result=gateway.status(job['id'])
            if result['state']=='reconciliation_required':break
            time.sleep(.01)
        self.assertFalse(result['passed']);self.assertFalse(gateway.clean())
        self.assertIn('Operation omitted real exit/output/cleanup evidence',Path(result['log']).read_text())
        self.assertFalse(gateway.stop())

    def test_exception_log_is_retrievable_while_effect_cleanup_stays_fenced(self):
        def failed(*_):raise RuntimeError('Owned diagnostic for manager reconciliation')
        self.gateway.handlers['build']=failed
        job=self.call({'operation':'build'});result=self.wait(job['id'])
        self.assertEqual(result['state'],'reconciliation_required')
        self.assertFalse(result['cleanup_verified']);self.assertFalse(self.gateway.clean())
        page=self.call({'operation':'logs','argument':job['id']+':0'})
        self.assertIn('Owned diagnostic for manager reconciliation',page['text'])


if __name__ == '__main__': unittest.main()
