from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from strata_board_harness.model_proxy import ModelProxy


class ModelProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.requests=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((body,self.headers.get('Authorization')))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                frames=[{'choices':[{'delta':{'content':'owned response'},'finish_reason':None}]},
                        {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':2,'prompt_tokens_details':{'cached_tokens':6}}}]
                for frame in frames:self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            def log_message(self,*_):pass
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.upstream.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.proxy=ModelProxy(f'http://127.0.0.1:{self.upstream.server_port}/v1','real-fixture-upstream-key','selected-model',self.root/'proxy')
        self.endpoint=self.proxy.start();self.addCleanup(self.proxy.close)

    def stop(self):self.upstream.shutdown();self.upstream.server_close();self.thread.join(timeout=2)

    def call(self,body=None,path='/chat/completions',token=None):
        request=Request(self.endpoint+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+(token or self.proxy.token),'Content-Type':'application/json'})
        with urlopen(request,timeout=3) as response:return response.read()

    def test_streaming_usage_and_actual_settings_are_recorded_without_prompt_or_credentials(self):
        body={'model':'selected-model','stream':True,'max_tokens':42,'messages':[{'role':'user','content':'private-fixture-prompt'}]}
        response=self.call(body)
        self.assertIn(b'[DONE]',response)
        for _ in range(100):
            records=[json.loads(line) for line in self.proxy.log.read_text().splitlines()]
            if records[-1].get('state')=='completed':break
            __import__('time').sleep(.01)
        self.assertEqual(records[-1]['usage']['prompt_tokens_details']['cached_tokens'],6)
        self.assertEqual(records[-1]['requested_settings']['chat_template_kwargs'],{'enable_thinking':False})
        self.assertTrue(records[-1]['requested_settings']['stream_options']['include_usage'])
        self.assertEqual(self.requests[0][1],'Bearer real-fixture-upstream-key')
        log=self.proxy.log.read_text()
        for secret in ['private-fixture-prompt','real-fixture-upstream-key',self.proxy.token]:self.assertNotIn(secret,log)

    def test_auth_control_routes_and_other_model_are_rejected_without_upstream_effect(self):
        with self.assertRaises(HTTPError) as error:self.call({'model':'selected-model'},token='wrong')
        self.assertEqual(error.exception.code,401);error.exception.close()
        with self.assertRaises(HTTPError) as error:self.call({'model':'selected-model'},path='/load')
        self.assertEqual(error.exception.code,404);error.exception.close()
        with self.assertRaises(HTTPError) as error:self.call({'model':'different-model','messages':[]})
        self.assertEqual(error.exception.code,503);error.exception.close()
        self.assertEqual(self.requests,[])

    def test_restart_retains_owned_endpoint_and_scoped_token(self):
        first=self.proxy.token;port=self.proxy.port
        self.proxy.close()
        second=ModelProxy(self.proxy.upstream,'real-fixture-upstream-key','selected-model',self.root/'proxy')
        second.start();self.addCleanup(second.close)
        self.assertEqual(second.token,first);self.assertEqual(second.port,port)


if __name__=='__main__':unittest.main()
