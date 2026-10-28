"""Pinned owning-host emulator bridge; no remote shell or arbitrary ADB API."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .gateway import JobGateway
from .managed_engine import write_private
from .supervisor import NoRedirect


# Uploaded APKs (60-110 MB per run) are only needed while their request runs; request.json
# and the job receipts stay for reconciliation.
KEEP_ARTIFACT_REQUESTS=3


def prune_artifacts(directory,keep):
    """Delete the APKs of all but the [keep] most recent request folders."""
    folders=sorted((path for path in Path(directory).iterdir()
                    if path.is_dir() and re.fullmatch('[0-9a-f]{64}',path.name)),
                   key=lambda path:path.stat().st_mtime,reverse=True)
    for folder in folders[keep:]:
        for name in ('app.apk','test.apk'):(folder/name).unlink(missing_ok=True)


class MacBridge:
    def __init__(self, runtime, directory, reservation, token=None):
        self.runtime,self.directory,self.reservation=runtime,Path(directory).resolve(),reservation
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.token=token or secrets.token_urlsafe(32)
        self.current=None;self.admission_lock=threading.Lock()
        self.gateway=JobGateway(self.directory/'jobs',{'test':self._test})
        self.server=None

    def _test(self,argument,cancelled):
        request=self.current
        return self.runtime.test_apks(self.directory/request['id']/'app.apk',
                                       self.directory/request['id']/'test.apk',cancelled,scenario=request['scenario'])

    def guard(self):
        path=Path(self.reservation['path'])/'owner.json'
        return path.exists() and json.loads(path.read_text())==self.reservation

    def lookup(self,identifier):
        if not re.fullmatch('[0-9a-f]{64}',identifier):raise ValueError('Invalid request ID')
        path=self.directory/identifier/'request.json'
        if not path.exists():return {'state':'not_observed'}
        saved=json.loads(path.read_text())
        if not saved.get('job_id'):return {'state':'reconciliation_required','request_id':identifier}
        return self.gateway.status(saved['job_id'])

    def admit(self,request):
        if (not isinstance(request,dict) or not {'request_id','app','test'}.issubset(request)
                or set(request)-{'request_id','app','test','scenario'}):raise ValueError('Invalid upload')
        scenario=request.get('scenario','normal')
        if scenario not in ('normal','denied') and (not isinstance(scenario,str) or not re.fullmatch(r'class:tech\.primal\.attentive_os\.[a-z][a-z0-9_]*\.[A-Za-z0-9_.]+',scenario)):
            raise ValueError('Invalid selected test scenario')
        identifier=request['request_id']
        if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{64}',identifier):raise ValueError('Invalid request ID')
        artifacts={}
        for name in ('app','test'):
            value=request[name]
            if not isinstance(value,dict) or set(value)!={'sha256','base64'}:raise ValueError('Invalid artifact')
            data=base64.b64decode(value['base64'],validate=True)
            if not data.startswith(b'PK') or len(data)>128*1024*1024 or hashlib.sha256(data).hexdigest()!=value['sha256']:
                raise ValueError('Artifact hash or format mismatch')
            artifacts[name]=data
        admission={'id':identifier,'scenario':scenario,'hashes':{name:hashlib.sha256(value).hexdigest() for name,value in artifacts.items()}}
        with self.admission_lock:
            folder=self.directory/identifier;path=folder/'request.json'
            if path.exists():
                saved=json.loads(path.read_text())
                if saved['hashes']!=admission['hashes'] or saved.get('scenario','normal')!=scenario:raise RuntimeError('Changed request identity')
                return self.lookup(identifier)
            if not self.guard() or not self.gateway.clean():raise RuntimeError('Exclusivity or cleanup not verified')
            prune_artifacts(self.directory,keep=KEEP_ARTIFACT_REQUESTS)
            folder.mkdir(mode=0o700)
            for name,data in artifacts.items():
                p=folder/(name+'.apk');p.write_bytes(data);p.chmod(0o600)
            write_private(path,admission)
            self.current=admission
            job=self.gateway.submit('test')
            admission['job_id']=job['id'];write_private(path,admission)
            return job

    def start(self,port=0):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def authenticated(self):
                if secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+owner.token):return True
                self.send_error(401);return False
            def answer(self,value):
                body=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_GET(self):
                if not self.authenticated():return
                try:
                    if self.path=='/guard':self.answer({'verified':owner.guard(),'reservation_nonce':owner.reservation['nonce'],
                        'runtime_clean':owner.runtime.clean()});return
                    if self.path.startswith('/request/'):
                        value=owner.lookup(self.path[len('/request/'):]);self.answer(value);return
                    if self.path.startswith('/log/'):
                        parts=self.path.split('/')
                        if len(parts)!=4:raise ValueError('Invalid log request')
                        value=owner.lookup(parts[2]);offset=int(parts[3])
                        if offset<0 or offset>10*1024*1024 or value.get('state')!='completed':raise ValueError('Invalid log offset')
                        path=Path(value['log']).resolve()
                        if not path.is_relative_to(owner.directory/'jobs'):raise ValueError('Unowned log')
                        text=path.read_text();chunk=text[offset:offset+16384]
                        self.answer({'text':chunk,'next_offset':offset+len(chunk) if offset+len(chunk)<len(text) else None});return
                    if self.path.startswith('/image/'):
                        value=owner.lookup(self.path[len('/image/'):]);info=value.get('screenshot')
                        if not info or value.get('state')!='completed':raise ValueError('No owned failure image')
                        path=Path(info['path']).resolve()
                        if not path.is_relative_to(owner.runtime.directory):raise ValueError('Unowned image')
                        data=path.read_bytes()
                        if len(data)>10*1024*1024 or hashlib.sha256(data).hexdigest()!=info['sha256']:raise ValueError('Image changed or oversized')
                        self.answer({'base64':base64.b64encode(data).decode(),'sha256':info['sha256']});return
                    if self.path=='/checkpoint':self.answer(owner.runtime.checkpoint());return
                    self.send_error(404)
                except (ValueError,RuntimeError,OSError,KeyError):self.send_error(409)
            def do_POST(self):
                if not self.authenticated():return
                try:
                    if self.path=='/resume':
                        if not owner.guard() or not owner.runtime.clean() or not owner.gateway.clean():
                            raise RuntimeError('Runtime cleanup not verified')
                        owner.gateway.cancel.clear();self.answer({'resumed':True});return
                    if self.path=='/cancel':
                        owner.gateway.cancel.set();owner.runtime.cancel();self.answer({'cancellation_requested':True});return
                    if self.path!='/run':self.send_error(404);return
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=352*1024*1024:raise ValueError('Bounded upload required')
                    result=owner.admit(json.loads(self.rfile.read(size)));self.answer(result)
                except (ValueError,RuntimeError,OSError,KeyError,TypeError) as error:
                    self.send_error(409,explain=(type(error).__name__+': '+str(error))[:300])
            def log_message(self,*_):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start();self.thread=thread
        return 'http://127.0.0.1:'+str(self.server.server_port)

    def close(self):
        self.gateway.stop(timeout=75)
        if self.server:self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        # Never clear cross-host exclusivity merely because this server exited.
        # The manager must verify the remote coding process/lock and these effects.
        return self.runtime.clean() and self.gateway.clean()


class BridgeRefused(RuntimeError):
    """An HTTP error answer from the bridge, with its reason when it sent one."""
    def __init__(self,code,reason=''):
        self.code,self.reason=code,reason
        super().__init__('Bridge HTTP '+str(code)+(': '+reason if reason else ''))


def upload_timeout(payload_bytes):
    """Seconds allowed for one artifact upload through the tunnel."""
    return max(120, 60 + payload_bytes // (256 * 1024))


class EmulatorClient:
    def __init__(self,config,receipts):
        parsed=urlsplit(config['endpoint'])
        if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path not in ('','/')
                or parsed.username or parsed.query or parsed.fragment):raise ValueError('Bridge must be loopback-only')
        self.config,self.directory=config,Path(receipts).resolve();self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.current=None
        path=self.directory/'bridge-request.json'
        if path.exists():self.current=json.loads(path.read_text())

    def _request(self,method,path,payload=None,timeout=10):
        request=Request(self.config['endpoint'].rstrip('/')+path,
            data=json.dumps(payload).encode() if payload is not None else None,method=method,
            headers={'Authorization':'Bearer '+self.config['token'],'Content-Type':'application/json'})
        try:
            with build_opener(NoRedirect()).open(request,timeout=timeout) as response:
                limit=16*1024*1024 if path.startswith('/image/') else 2*1024*1024
                body=response.read(limit+1)
                if len(body)>limit:raise RuntimeError('Oversized bridge response')
                return json.loads(body)
        except HTTPError as error:
            code=error.code
            try:detail=re.sub(r'<[^>]+>',' ',error.read(4096).decode(errors='replace'))
            except OSError:detail=''
            error.close()
            reason=re.search(r'Message:\s*([^\n]*?)\s+Error code explanation:\s*409\s*-\s*([^\n.]*)',detail)
            raise BridgeRefused(code,(reason.group(2) if reason else '').strip()) from None
        except (URLError,OSError,ValueError):raise RuntimeError('Bridge unavailable or response invalid') from None

    def guard(self):
        result=self._request('GET','/guard',timeout=5)
        return result.get('verified') is True and result.get('reservation_nonce')==self.config['reservation_nonce']

    def checkpoint(self):return self._request('GET','/checkpoint',timeout=5)
    def clean(self):return self.checkpoint().get('cleanup_verified') is True

    def resume(self):
        result=self._request('POST','/resume',{},timeout=10)
        if result.get('resumed') is not True:raise RuntimeError('Bridge not resumed')

    def cancel(self):
        self._request('POST','/cancel',{},timeout=10)
        deadline=time.monotonic()+75
        while time.monotonic()<deadline:
            if self.clean():return True
            time.sleep(.25)
        return False

    def test(self,workspace,cancelled,scenario='normal'):
        workspace=Path(workspace)
        paths={'app':workspace/'app/build/outputs/apk/debug/app-debug.apk',
               'test':workspace/'app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'}
        artifacts={}
        for name,path in paths.items():
            data=path.read_bytes()
            if len(data)>128*1024*1024:raise ValueError('APK too large')
            artifacts[name]={'sha256':hashlib.sha256(data).hexdigest(),'base64':base64.b64encode(data).decode()}
        identifier=hashlib.sha256(json.dumps([self.config['reservation_nonce'],scenario,artifacts['app']['sha256'],artifacts['test']['sha256']]).encode()).hexdigest()
        saved_path=self.directory/'bridge-request.json'
        saved=json.loads(saved_path.read_text()) if saved_path.exists() else None
        observed=self._request('GET','/request/'+identifier)
        if observed['state']=='not_observed':
            if saved and saved.get('request_id')==identifier:
                raise RuntimeError('Previous test admission uncertain; do not resubmit')
            self.current={'request_id':identifier,'state':'prepared'};write_private(saved_path,self.current)
            payload={'request_id':identifier,'scenario':scenario,**artifacts}
            # Base64 APKs cross the SSH tunnel: a 100 MB debug APK is ~140 MB on the wire, far
            # beyond a fixed 120 s. Budget a conservative 256 KiB/s with the old floor.
            upload=sum(len(item['base64']) for item in artifacts.values())
            try:result=self._request('POST','/run',payload,timeout=upload_timeout(upload))
            except BridgeRefused as refusal:
                # The bridge validates before it stores anything: a refusal leaves no admitted
                # request. Confirm that, then report an ordinary failure instead of an
                # uncertain admission that would fence every later run.
                if refusal.code!=409 or self._request('GET','/request/'+identifier)['state']!='not_observed':raise
                saved_path.unlink(missing_ok=True);self.current=None
                return {'exit_code':78,'cleanup_verified':True,'scenario':scenario,
                        'output':'The emulator bridge refused this run before admitting it (HTTP 409): '
                                 +(refusal.reason or 'no reason given')+'. Nothing ran; retry after fixing the cause.'}
            self.current['state']='admitted';write_private(saved_path,self.current)
        else:result=observed
        deadline=time.monotonic()+1600
        while result['state'] in ('prepared','running'):
            if cancelled.wait(.25):self.cancel()
            if time.monotonic()>=deadline:raise RuntimeError('Bridge result requires timeout reconciliation')
            result=self._request('GET','/request/'+identifier)
        if result.get('state')!='completed' or result.get('cleanup_verified') is not True:
            raise RuntimeError('Bridge runtime/cleanup outcome requires reconciliation')
        text='';offset=0
        while offset is not None:
            page=self._request('GET','/log/'+identifier+'/'+str(offset))
            if not isinstance(page.get('text'),str):raise RuntimeError('Invalid runtime log page')
            text+=page['text'];offset=page.get('next_offset')
            if len(text)>10*1024*1024:raise RuntimeError('Runtime log exceeds bounded retrieval')
        return {**result,'output':text}

    def failure_image(self,workspace):
        if not self.current:raise RuntimeError('No current owned runtime request')
        info=self._request('GET','/image/'+self.current['request_id'])
        data=base64.b64decode(info['base64'],validate=True)
        if hashlib.sha256(data).hexdigest()!=info['sha256'] or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('Invalid failure image')
        path=Path(workspace)/'.harness/screenshots'/(self.current['request_id']+'.png')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o600)
        return {'path':str(path),'sha256':info['sha256']}
