"""Loopback model proxy for one run: the coding process holds only a throwaway token.

Any AI SDK package works: the request path is forwarded unchanged under the
upstream base URL, and the throwaway token is swapped for the real key in
whichever header the SDK used (Authorization, x-api-key, x-goog-api-key, api-key).
Only sizes, status codes and timings are logged; never bodies or keys.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

KEY_HEADERS = ('authorization', 'x-api-key', 'x-goog-api-key', 'api-key')
PASS_HEADERS = ('content-type', 'accept', 'anthropic-version', 'anthropic-beta', 'openai-beta', 'user-agent')
LIMIT = 64 * 1024 * 1024


def without_history_thinking(body):
    """The request without assistant reasoning parts in its history, or None if it has none."""
    try:
        data = json.loads(body or b'')
    except ValueError:
        return None
    changed = False
    for message in data.get('messages') or [] if isinstance(data, dict) else []:
        content = message.get('content') if isinstance(message, dict) and message.get('role') == 'assistant' else None
        if isinstance(content, list):
            kept = [part for part in content if not (isinstance(part, dict) and part.get('type') in ('thinking', 'reasoning'))]
            if len(kept) != len(content):
                message['content'] = kept or ''
                changed = True
    return json.dumps(data).encode() if changed else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RunProxy:
    def __init__(self, upstream, key, log_path):
        self.upstream = upstream.rstrip('/')
        self.prefix = urlsplit(self.upstream).path
        self.key = key
        self.token = 'harness-' + secrets.token_urlsafe(24)
        self.log_path = log_path
        self.lock = threading.Lock()
        self.opener = build_opener(_NoRedirect())
        self.server = None
        # Some endpoints reject the reasoning ("thinking") parts an SDK echoes back in the history.
        # Learned per run: after one such 5xx is cured by removing them, they are always removed.
        self.strip_thinking = False

    def _record(self, value):
        with self.lock, open(self.log_path, 'a') as stream:
            stream.write(json.dumps(value) + '\n')

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def _forward(self):
                started, record = time.time(), {'method': self.command, 'path': self.path.split('?')[0]}
                header = next((name for name in KEY_HEADERS
                               if owner.token in (self.headers.get(name) or '')), None)
                if header is None:
                    self.send_error(401)
                    return
                if not self.path.startswith(owner.prefix + '/') and owner.prefix:
                    self.send_error(404)
                    return
                size = int(self.headers.get('Content-Length') or 0)
                if size > LIMIT:
                    self.send_error(413)
                    return
                body = self.rfile.read(size) if size else None
                headers = {name: self.headers[name] for name in PASS_HEADERS if self.headers.get(name)}
                if owner.key:
                    headers[header] = self.headers[header].replace(owner.token, owner.key)
                target = owner.upstream + self.path[len(owner.prefix):]
                record['request_bytes'] = size
                stripped = without_history_thinking(body)
                if owner.strip_thinking and stripped is not None:
                    body, stripped, record['stripped_thinking'] = stripped, None, True

                def send(data):
                    try:
                        answer = owner.opener.open(Request(target, data=data, method=self.command, headers=headers),
                                                   timeout=300)
                        return answer, answer.status
                    except HTTPError as error:
                        return error, error.code

                try:
                    response, status = send(body)
                    if status >= 500 and stripped is not None:
                        retry, retry_status = send(stripped)
                        if retry_status < 500:
                            response.close()
                            response, status = retry, retry_status
                            owner.strip_thinking = record['stripped_thinking'] = True
                        else:
                            retry.close()
                except (URLError, OSError) as error:
                    record.update(status=502, error=type(error).__name__, seconds=round(time.time() - started, 2))
                    owner._record(record)
                    self.send_error(502)
                    return
                with response:
                    self.send_response(status)
                    for name in ('Content-Type', 'Cache-Control', 'retry-after'):
                        if response.headers.get(name):
                            self.send_header(name, response.headers[name])
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    total = 0
                    try:
                        while chunk := response.read1(65536):
                            total += len(chunk)
                            self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                            self.wfile.flush()
                        self.wfile.write(b'0\r\n\r\n')
                    except (BrokenPipeError, ConnectionResetError):
                        record['client_disconnected'] = True
                record.update(status=status, response_bytes=total, seconds=round(time.time() - started, 2))
                owner._record(record)
                self.close_connection = True

            do_GET = do_POST = _forward

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.server.server_port}{self.prefix}'

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
