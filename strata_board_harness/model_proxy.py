"""A loopback, model-scoped recording proxy; never exposes upstream control APIs.

Record requested generation settings and raw usage for every coding/auxiliary
request, without logging prompt/tool bodies or either bearer credential.
"""

from datetime import datetime,timezone
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from urllib.request import Request,build_opener
from uuid import uuid4

from .managed_engine import write_private
from .supervisor import NoRedirect


class ModelProxy:
    def __init__(self,upstream,key,model,directory):
        parsed=urlsplit(upstream)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username
                or parsed.query or parsed.fragment or parsed.path.rstrip('/')!='/v1'):
            raise ValueError('Explicit authenticated upstream v1 endpoint required')
        self.upstream,self.key,self.model=upstream.rstrip('/'),key,model
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.Lock();self.server=None
        saved=self.directory/'proxy.private.json'
        value=json.loads(saved.read_text()) if saved.exists() else {'token':secrets.token_urlsafe(32),'port':0}
        self.token,self.port=value['token'],value['port']
        self.log=self.directory/'requests.jsonl'

    def record(self,value):
        with self.lock:
            with self.log.open('a') as stream:
                self.log.chmod(0o600);stream.write(json.dumps(value,ensure_ascii=False)+'\n')

    def start(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def authenticated(self):
                if secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+owner.token):return True
                self.send_error(401);return False
            def do_GET(self):
                if not self.authenticated():return
                if self.path!='/v1/models':self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'object':'list','data':[{'id':owner.model,'object':'model'}]}).encode())
            def do_POST(self):
                if not self.authenticated():return
                if self.path!='/v1/chat/completions':self.send_error(404);return
                started=time.time();receipt={'id':uuid4().hex,'at':datetime.now(timezone.utc).isoformat(),
                    'state':'prepared','configured_api_billing_usd':0}
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=16*1024*1024:raise ValueError('Bounded model request required')
                    raw=self.rfile.read(size);body=json.loads(raw)
                    if not isinstance(body,dict) or body.get('model')!=owner.model:raise ValueError('Unselected model')
                    receipt['client_body_sha256']=hashlib.sha256(raw).hexdigest()
                    template=body.setdefault('chat_template_kwargs',{})
                    if not isinstance(template,dict):raise ValueError('Invalid template options')
                    template['enable_thinking']=False
                    if body.get('stream') is True:
                        stream_options=body.setdefault('stream_options',{})
                        if not isinstance(stream_options,dict):raise ValueError('Invalid stream options')
                        stream_options['include_usage']=True
                    raw=json.dumps(body).encode()
                    keys=('model','temperature','top_p','top_k','max_tokens','max_completion_tokens','stream',
                          'chat_template_kwargs','stream_options','reasoning_effort','reasoning','presence_penalty','frequency_penalty','seed')
                    receipt['requested_settings']={k:body[k] for k in keys if k in body}
                    receipt['body_sha256']=hashlib.sha256(raw).hexdigest()
                    receipt['message_count']=len(body.get('messages',[]))
                    receipt['tool_count']=len(body.get('tools',[]))
                    owner.record(dict(receipt))
                    request=Request(owner.upstream+'/chat/completions',data=raw,
                        headers={'Authorization':'Bearer '+owner.key,'Content-Type':'application/json'})
                    with build_opener(NoRedirect()).open(request,timeout=120) as response:
                        receipt['http_status']=response.status
                        self.send_response(response.status);self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                        self.end_headers()
                        pending=b'';total=0;streaming=body.get('stream') is True
                        while True:
                            chunk=response.read1(8192)
                            if not chunk:break
                            total+=len(chunk)
                            if total>16*1024*1024:raise ValueError('Bounded model response exceeded')
                            self.wfile.write(chunk);self.wfile.flush();pending+=chunk
                            if streaming:
                                lines=pending.split(b'\n');pending=lines.pop()
                                for line in lines:
                                    if not line.startswith(b'data: '):continue
                                    try:value=json.loads(line[6:])
                                    except (ValueError,UnicodeError):continue
                                    if value.get('usage'):receipt['usage']=value['usage']
                        if not streaming and pending:
                            try:value=json.loads(pending)
                            except (ValueError,UnicodeError):value={}
                            if value.get('usage'):receipt['usage']=value['usage']
                    receipt['state']='completed'
                except HTTPError as error:
                    code=error.code;payload=error.read(32768);error.close()
                    receipt.update(state='configuration_error' if code in (301,302,303,307,308,401,403) else 'upstream_error',http_status=code)
                    self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):receipt['state']='client_disconnected'
                except (OSError,URLError,ValueError,TypeError):
                    receipt['state']='upstream_unavailable_or_invalid'
                    try:self.send_error(503)
                    except OSError:pass
                finally:
                    receipt['seconds']=time.time()-started
                    owner.record(receipt)
        self.server=ThreadingHTTPServer(('127.0.0.1',self.port),Handler)
        self.port=self.server.server_port
        write_private(self.directory/'proxy.private.json',{'token':self.token,'port':self.port})
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return 'http://127.0.0.1:'+str(self.port)+'/v1'

    def close(self):
        if self.server:self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
