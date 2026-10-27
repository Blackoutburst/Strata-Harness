"""A scoped, authenticated async job gateway; no shell command API.

Adapters provide fixed operations. Requests cannot choose argv, environment,
working directory, a serial or a remote host. Worker jobs never share SQLite
connections with the Board/control thread.
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
import traceback
from pathlib import Path

from .managed_engine import write_private


class JobGateway:
    def __init__(self, directory, handlers, token=None, clock=time.time):
        if not handlers or any(name not in ('build', 'test', 'screenshot', 'checkpoint', 'commit')
                               or not callable(handler) for name, handler in handlers.items()):
            raise ValueError('Explicit fixed operation handlers required')
        self.directory = Path(directory).resolve();self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handlers, self.token, self.clock = handlers, token or secrets.token_urlsafe(32), clock
        self.lock = threading.Lock();self.jobs = {};self.threads = {};self.cancel = threading.Event()
        self.server = self.thread = None
        # Previous jobs are not assumed stopped merely because the server restarted.
        for path in self.directory.glob('job-*.json'):
            value = json.loads(path.read_text())
            if value.get('state') in ('prepared', 'running'):
                raise RuntimeError('Previous gateway job requires process/effect reconciliation')
            self.jobs[value['id']] = value

    def submit(self, operation, argument=None):
        if operation not in self.handlers: raise ValueError('Operation outside selected scope')
        test_argument = (operation == 'test' and isinstance(argument,str) and (argument=='denied' or
            __import__('re').fullmatch(r'class:tech\.primal\.attentive_os\.[a-z][a-z0-9_]*\.[A-Za-z0-9_.]+',argument)))
        if argument is not None and not test_argument and (operation != 'commit' or not isinstance(argument, str)
                                     or not 1 <= len(argument) <= 2048):
            raise ValueError('Unexpected operation argument')
        with self.lock:
            if self.cancel.is_set(): raise RuntimeError('Gateway stopping')
            if any(job['state'] in ('prepared', 'running') for job in self.jobs.values()):
                raise RuntimeError('One owned validation operation at a time')
            identifier = 'job-' + secrets.token_hex(16)
            job = {'id': identifier, 'operation': operation, 'state': 'prepared',
                   'argument': argument, 'started_at': self.clock()}
            self.jobs[identifier] = job;self._save(job)
            thread = threading.Thread(target=self._execute, args=(identifier,), daemon=True)
            self.threads[identifier] = thread;thread.start()
            return dict(job)

    def _save(self, job): write_private(self.directory / (job['id'] + '.json'), job)

    def _execute(self, identifier):
        with self.lock:
            job = self.jobs[identifier];job['state'] = 'running';self._save(job)
        try:
            result = self.handlers[job['operation']](job['argument'], self.cancel)
            if (not isinstance(result, dict) or type(result.get('exit_code')) is not int
                    or not isinstance(result.get('output'), str) or result.get('cleanup_verified') is not True):
                raise RuntimeError('Operation omitted real exit/output/cleanup evidence')
            output = result.pop('output')
            # Handler results (e.g. the bridge's own job record) must not overwrite this job's identity.
            for key in ('id', 'operation', 'argument', 'started_at'):
                if key in result: result['handler_' + key] = result.pop(key)
            path = self.directory / (identifier + '.log')
            with path.open('w') as stream: path.chmod(0o600);stream.write(output)
            outcome = {**result, 'state': 'completed', 'passed': result['exit_code'] == 0,
                'log': str(path), 'log_sha256': hashlib.sha256(output.encode()).hexdigest(),
                'output_tail': output[-24000:], 'output_bytes': len(output.encode())}
        except Exception as error:
            path = self.directory / (identifier + '.log')
            diagnostic = traceback.format_exc()
            with path.open('w') as stream: path.chmod(0o600);stream.write(diagnostic)
            outcome = {'state': 'reconciliation_required', 'passed': False,
                       'error_type': type(error).__name__, 'cleanup_verified': False,
                       'log':str(path),'log_sha256':hashlib.sha256(diagnostic.encode()).hexdigest(),
                       'output_bytes':len(diagnostic.encode())}
        with self.lock:
            job.update(outcome, finished_at=self.clock());self._save(job)

    def status(self, identifier=None):
        with self.lock:
            if identifier:
                if identifier not in self.jobs: raise ValueError('Unknown owned job')
                return dict(self.jobs[identifier])
            return {'jobs': [{k:v for k,v in value.items() if k not in ('output_tail','argument')}
                             for value in self.jobs.values()], 'stopping': self.cancel.is_set()}

    def activity(self):
        with self.lock:
            for identifier,thread in self.threads.items():
                job=self.jobs[identifier]
                if thread.is_alive() and job['state'] in ('prepared','running'):
                    cap=1805 if job['operation']=='test' else 905
                    return {'operation':job['operation'],'started_at':job['started_at'],
                            'deadline_at':job['started_at']+cap,'verified_thread_alive':True}
        return None

    def clean(self):
        with self.lock:
            return all(not thread.is_alive() for thread in self.threads.values()) and all(
                job.get('cleanup_verified') is True for job in self.jobs.values())

    def start(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != '/operation': self.send_error(404);return
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + owner.token):
                    self.send_error(401);return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 4096: raise ValueError('Bounded request required')
                    request = json.loads(self.rfile.read(size))
                    if not isinstance(request, dict) or set(request) - {'operation', 'argument'}:
                        raise ValueError('Invalid request')
                    op, arg = request.get('operation'), request.get('argument')
                    if op == 'logs':
                        if not isinstance(arg,str):raise ValueError('Owned job ID required')
                        identifier,_,position=arg.partition(':');offset=int(position or '0')
                        saved=owner.status(identifier)
                        if saved.get('state') not in ('completed','reconciliation_required') or 'log' not in saved or not 0<=offset<=10*1024*1024:
                            raise ValueError('Finished owned log and bounded offset required')
                        text=Path(saved['log']).read_text();chunk=text[offset:offset+16384]
                        result={'job_id':identifier,'text':chunk,'next_offset':offset+len(chunk) if offset+len(chunk)<len(text) else None}
                    elif op == 'status':
                        result = owner.status(arg)
                    else: result = owner.submit(op, arg)
                    payload = json.dumps(result).encode()
                    self.send_response(200);self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)));self.end_headers();self.wfile.write(payload)
                except (ValueError, RuntimeError, TypeError, KeyError): self.send_error(409)
            def log_message(self, *_): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self, timeout=10):
        self.cancel.set()
        deadline = time.monotonic() + timeout
        for thread in list(self.threads.values()): thread.join(max(0, deadline - time.monotonic()))
        if self.server: self.server.shutdown();self.server.server_close()
        if self.thread: self.thread.join(timeout=2)
        return self.clean()
