import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from strata_board_harness.bridge import MacBridge,EmulatorClient
from strata_board_harness.managed_engine import write_private


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);lock=self.root/'reservation';lock.mkdir()
        self.reservation={'path':str(lock),'nonce':'owned-reservation','purpose':'selected-pilot'}
        write_private(lock/'owner.json',self.reservation)
        class Runtime:
            count=0
            def test_apks(self,app,test,cancelled,scenario='normal'):
                self.count+=1
                return {'exit_code':1,'output':'one pass, one fail','cleanup_verified':True,'instrumentation':{'failed':1}}
            def clean(self):return True
            def checkpoint(self):return {'cleanup_verified':True,'phase':'idle'}
            def cancel(self):pass
        self.runtime=Runtime();self.bridge=MacBridge(self.runtime,self.root/'bridge',self.reservation)
        self.endpoint=self.bridge.start();self.addCleanup(self.bridge.close)
        self.client=EmulatorClient({'endpoint':self.endpoint,'token':self.bridge.token,
            'reservation_nonce':self.reservation['nonce']},self.root/'client')
        self.app=b'PKowned-app';self.test=b'PKowned-test'
        def artifact(data):return {'sha256':hashlib.sha256(data).hexdigest(),'base64':base64.b64encode(data).decode()}
        self.request={'request_id':'a'*64,'app':artifact(self.app),'test':artifact(self.test)}

    def test_duplicate_admission_after_lost_reply_never_repeats_runtime(self):
        first=self.bridge.admit(self.request)
        for _ in range(50):
            second=self.bridge.admit(self.request)
            if second['state']=='completed':break
            time.sleep(.01)
        self.assertEqual(first['id'],second['id']);self.assertEqual(self.runtime.count,1)
        self.assertEqual(second['exit_code'],1);self.assertFalse(second['passed'])
        self.assertTrue(self.client.guard());self.assertTrue(self.client.clean())

    def test_changed_artifact_under_same_id_and_unknown_request_fields_are_rejected(self):
        self.bridge.admit(self.request)
        changed=json.loads(json.dumps(self.request));changed['app']['base64']=base64.b64encode(b'PKchanged').decode()
        changed['app']['sha256']=hashlib.sha256(b'PKchanged').hexdigest()
        with self.assertRaises(RuntimeError):self.bridge.admit(changed)
        with self.assertRaises(ValueError):self.bridge.admit(dict(self.request,serial='physical'))

    def test_client_pins_two_apks_and_preserves_failure_without_duplicate_runtime(self):
        work=self.root/'workspace';app=work/'app/build/outputs/apk/debug/app-debug.apk'
        test=work/'app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'
        for p,data in ((app,self.app),(test,self.test)):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        result=self.client.test(work,threading.Event())
        self.assertEqual(result['exit_code'],1);self.assertTrue(result['cleanup_verified'])
        self.client.test(work,threading.Event());self.assertEqual(self.runtime.count,1)

    def test_auth_and_changed_global_reservation_fail_closed(self):
        request=Request(self.endpoint+'/guard',headers={'Authorization':'Bearer wrong'})
        with self.assertRaises(HTTPError) as error:urlopen(request,timeout=3)
        self.assertEqual(error.exception.code,401);error.exception.close()
        write_private(Path(self.reservation['path'])/'owner.json',dict(self.reservation,nonce='other'))
        self.assertFalse(self.client.guard())


if __name__=='__main__':unittest.main()
