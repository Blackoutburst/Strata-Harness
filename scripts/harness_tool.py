#!/usr/bin/env python3
"""Worker-facing fixed operation client. Never runs a caller's shell command."""
import json
import os
import sys
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


DIGEST_CHARS = 8000


def digest(result):
    """Bounded view of a finished job for the model's context.

    Raw output (full dumpsys dumps, Gradle logs) can be tens of thousands of tokens per
    call and once overflowed the model's context window. The full log stays available
    through `harness-tool logs <job_id>:<page>`.
    """
    view = {key: value for key, value in result.items() if key not in ('output_tail', 'output')}
    text = result.get('output_tail') or ''
    lines = text.splitlines()
    picked = []
    if result.get('operation') == 'test':
        # Failure headers plus the first lines of each stack, then the test's own evidence.
        for index, line in enumerate(lines):
            if line.startswith('INSTRUMENTATION_STATUS: stack='):
                picked += lines[index:index + 6]
        picked += [line for line in lines if 'E2E_DUMP' in line or 'AndroidRuntime' in line][-60:]
        picked += [line for line in lines if line.startswith(('Tests run:', 'OK (', 'FAILURES'))]
    else:
        picked += [line for line in lines if line.startswith(('e: ', 'w: file')) or 'error:' in line
                   or 'What went wrong' in line or 'FAILED' in line][:80]
        picked += lines[-25:]
    excerpt = '\n'.join(dict.fromkeys(picked))
    if len(excerpt) > DIGEST_CHARS:
        excerpt = excerpt[:DIGEST_CHARS // 2] + '\n[... trimmed ...]\n' + excerpt[-DIGEST_CHARS // 2:]
    view['output_excerpt'] = excerpt
    if result.get('id'): view['full_log'] = 'harness-tool logs %s:0 (next pages :1, :2, ...)' % result['id']
    return view


def main():
    if not 2 <= len(sys.argv) <= 3 or sys.argv[1] not in ('status','logs','build','test','screenshot','checkpoint','commit'):
        raise SystemExit('Use one fixed harness-tool operation')
    endpoint = os.environ['HARNESS_GATEWAY_URL'];parts = urlsplit(endpoint)
    if parts.scheme != 'http' or parts.hostname != '127.0.0.1' or parts.path or parts.query or parts.username:
        raise SystemExit('Gateway must be owned loopback HTTP')
    def call(operation, argument=None):
        request = Request(endpoint + '/operation', data=json.dumps({'operation': operation, 'argument': argument}).encode(),
            headers={'Authorization': 'Bearer ' + os.environ['HARNESS_GATEWAY_TOKEN'], 'Content-Type': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024: raise RuntimeError('Oversized gateway response')
            return json.loads(body)
    op = sys.argv[1]
    result = call(op, sys.argv[2] if len(sys.argv) == 3 else None)
    if op not in ('status', 'logs'):
        deadline = time.monotonic() + (1805 if op == 'test' else 905)
        while result['state'] in ('prepared', 'running') and time.monotonic() < deadline:
            time.sleep(1)
            result = call('status', result['id'])
        if result['state'] in ('prepared', 'running'):
            print(json.dumps({'state':'still_running','job_id':result['id'],'passed':False}));return 124
    print(json.dumps(digest(result) if op not in ('status', 'logs') else result, ensure_ascii=False))
    if result.get('state') == 'completed': return result['exit_code'] if 0 <= result['exit_code'] <= 255 else 1
    return 1 if result.get('state') == 'reconciliation_required' else 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError):
        print('Scoped gateway unavailable or response invalid; operation outcome requires checking', file=sys.stderr)
        raise SystemExit(125)
