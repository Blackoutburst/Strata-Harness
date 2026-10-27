import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from strata_board_harness.execution import Validation


class ValidationGatewayTests(unittest.TestCase):
    def test_readonly_handlers_verify_effects_without_waiting_for_their_own_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            builds=SimpleNamespace(workspace=Path(tmp),clean=lambda:True,
                checkpoint=lambda **_: {'head':'owned','source_sha256':'hash'},
                build=lambda *_:None,test=lambda *_:None,commit=lambda *_:None)
            bridge=SimpleNamespace(failure_image=lambda _: {'path':str(Path(tmp)/'owned.png')})
            validation=Validation(builds,bridge,Path(tmp)/'jobs')
            try:
                for operation in ['checkpoint','screenshot']:
                    job=validation.gateway.submit(operation)
                    deadline=time.monotonic()+2
                    while time.monotonic()<deadline:
                        value=validation.gateway.status(job['id'])
                        if value['state'] not in ('prepared','running'):break
                        time.sleep(.01)
                    self.assertEqual(value['state'],'completed')
                    self.assertEqual(value['exit_code'],0)
                    self.assertTrue(value['cleanup_verified'])
                    self.assertTrue(json.loads(Path(value['log']).read_text()))
            finally:self.assertTrue(validation.close())

    def test_reviewed_control_hash_resolves_request_without_turning_old_build_into_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            changes=[{'path':'app/build.gradle.kts','sha256':'selected'}]
            builds=SimpleNamespace(workspace=Path(tmp),clean=lambda:True,status=lambda:{},
                checkpoint=lambda **_: {'source_sha256':'current'},control_changes=lambda:list(changes),
                build=lambda *_:{'exit_code':78,'output':'Exact control approval required','cleanup_verified':True,
                    'requires_manager_review':list(changes)},test=lambda *_:None,commit=lambda *_:None)
            validation=Validation(builds,SimpleNamespace(),Path(tmp)/'jobs')
            try:
                job=validation.gateway.submit('build');deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    result=validation.gateway.status(job['id'])
                    if result['state']=='completed':break
                    time.sleep(.01)
                self.assertEqual(validation.status()['requires_manager_review'],changes)
                changes.clear()
                self.assertNotIn('requires_manager_review',validation.status())
                self.assertFalse(validation.gateway.status(job['id'])['passed'])
                self.assertNotIn('successful_build_at',validation.status())
            finally:self.assertTrue(validation.close())


if __name__=='__main__':unittest.main()
