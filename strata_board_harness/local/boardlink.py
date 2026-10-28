"""Board access for local agents: pairing, human grants on a no-login board, and MCP calls.

A local board started without login (BOARD_REQUIRE_LOGIN=0) acts as its owner for
loopback requests. Setup uses that to pair an agent and grant it worker permission;
the running agent itself only ever uses its own paired MCP credential.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class BoardError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = build_opener(_NoRedirect())


def board_url(url):
    parts = urlsplit(url)
    local = parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1')
    if not parts.hostname or not (local or parts.scheme == 'https') or parts.username or parts.query or parts.fragment:
        raise ValueError('Board URL must be HTTPS or loopback HTTP without credentials or query')
    return url.rstrip('/')


def _http(method, url, body=None, headers=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    request = Request(url, data=data, method=method, headers={
        'Accept': 'application/json, text/event-stream', **({'Content-Type': 'application/json'} if data else {}),
        **(headers or {})})
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024)
    except HTTPError as error:
        detail = error.read(4096).decode(errors='replace')
        error.close()
        raise BoardError(f'{method} {urlsplit(url).path} returned HTTP {error.code}: {detail[:500]}') from None
    except (URLError, OSError) as error:
        raise BoardError(f'Board unreachable at {url}: {error}') from None
    return json.loads(raw) if raw else None


class LocalOwner:
    """The board owner, reachable without login on a loopback board."""

    def __init__(self, url):
        self.url = board_url(url)
        me = _http('GET', self.url + '/api/me')
        if me.get('login_required', True):
            raise BoardError('This board requires login; pair agents from its Connect agent page instead')
        self.owner_id = me['actor']['owner_id']
        self.headers = {'X-CSRF-Token': me['csrf_token'], 'Origin': self.url}

    def call(self, method, path, body=None):
        return _http(method, self.url + '/api' + path, body, self.headers if method != 'GET' else None)

    def pair(self, label):
        code = self.call('POST', '/settings/pairings', {'label': label[:100]})['code']
        paired = _http('POST', self.url + '/mcp/connect/exchange', {'code': code})
        if paired.get('mcp_url') != self.url + '/mcp':
            raise BoardError('The board returned a different MCP address')
        return paired

    def enable_agents(self):
        """Owner-level switch: agents may read, reply and take work at all."""
        settings = self.call('GET', '/settings')
        policy = settings.get('policy') or {}
        if policy.get('enabled') and policy.get('allow_read') and policy.get('allow_reply') and policy.get('allow_requests'):
            return
        self.call('PUT', '/settings/policy', {
            'enabled': True, 'allow_read': True, 'allow_reply': True, 'allow_requests': True,
            'actions': policy.get('actions') or [], 'resources': policy.get('resources') or []})

    def grant_worker(self, profile_id, project_ids=None):
        """Let one agent profile read, discuss, and take queued work (optionally only some projects)."""
        profile = self.call('GET', f'/agent-profiles/{profile_id}')
        profile = profile.get('profile', profile)
        changes = {'policy': {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
                              'actions': [], 'resources': [], 'autonomy': 'queue'}}
        if project_ids is not None:
            changes['project_ids'] = list(project_ids)
        return self.call('PATCH', f'/agent-profiles/{profile_id}', {'expected_version': profile['version'], **changes})


class AgentSession:
    """One registered MCP session for a paired agent identity."""

    def __init__(self, credential, identity_key, name, label='Harness agent', autonomous=True, skills=()):
        self.mcp_url, self.token = credential['mcp_url'], credential['token']
        self.session_id = 'harness-' + uuid4().hex
        self._rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                 'clientInfo': {'name': 'strata-harness', 'version': '1'}})
        self.registration = self.tool('register_agent', label=label, agent_kind='other', identity_key=identity_key,
                                      name=name, skills=list(skills), autonomous=autonomous, view='full')
        self.profile_id = self.registration['profile']['id']

    def _rpc(self, method, params):
        payload = {'jsonrpc': '2.0', 'id': uuid4().hex, 'method': method, 'params': params}
        data = _http('POST', self.mcp_url, payload, {'Authorization': 'Bearer ' + self.token})
        if not isinstance(data, dict) or 'error' in data:
            raise BoardError(f'MCP {method} failed: {(data or {}).get("error")}')
        return data['result']

    def tool(self, tool_name, **arguments):
        arguments.setdefault('session_id', self.session_id)
        result = self._rpc('tools/call', {'name': tool_name, 'arguments': arguments})
        if result.get('isError'):
            text = ' '.join(part.get('text', '') for part in result.get('content', []))
            raise BoardError(f'{tool_name}: {text[:800]}')
        data = result.get('structuredContent')
        if data is None:
            data = json.loads(result['content'][0]['text'])
        if isinstance(data, dict) and set(data) == {'result'}:
            data = data['result']
        return data
