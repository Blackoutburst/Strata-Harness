"""Where an agent's model comes from. Nothing here names a provider.

An agent's model is described by an OpenAI-, Anthropic- or other AI-SDK-compatible
endpoint: base URL, AI SDK package, model id, model settings, and where its key is.
That description is taken from the user's own OpenCode setup (any provider they
configured or logged into, including custom and local ones), or given explicitly.
Keys are read only when a run starts and are handed only to the loopback proxy.
"""

import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_NPM = '@ai-sdk/openai-compatible'
DEFAULT_LIMIT = {'context': 131072, 'output': 32768}


def _home(variable, *default):
    return Path(os.environ.get(variable) or Path.home().joinpath(*default))


def opencode_config_files():
    root = _home('XDG_CONFIG_HOME', '.config') / 'opencode'
    return [root / name for name in ('config.json', 'opencode.json', 'opencode.jsonc')]


def opencode_auth_file():
    return _home('XDG_DATA_HOME', '.local', 'share') / 'opencode' / 'auth.json'


def opencode_models_file():
    return _home('XDG_CACHE_HOME', '.cache') / 'opencode' / 'models.json'


def load_jsonc(text):
    """JSON with comments and trailing commas, as OpenCode accepts."""
    out, i, quoted = [], 0, False
    while i < len(text):
        char = text[i]
        if quoted:
            out.append(char)
            if char == '\\':
                out.append(text[i + 1:i + 2])
                i += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif text.startswith('//', i):
            i = text.find('\n', i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith('/*', i):
            i = text.find('*/', i)
            i = len(text) if i < 0 else i + 2
            continue
        else:
            out.append(char)
        i += 1
    return json.loads(re.sub(r',(\s*[}\]])', r'\1', ''.join(out)))


def _read(path, jsonc=False):
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        return load_jsonc(text) if jsonc else json.loads(text)
    except ValueError:
        return {}


def _user_providers():
    providers = {}
    for path in opencode_config_files():
        providers.update(_read(path, jsonc=True).get('provider') or {})
    return providers


def _env_reference(value):
    match = re.fullmatch(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', value or '')
    return match.group(1) if match else None


def from_opencode(reference):
    """Describe `provider/model` exactly as the user's OpenCode would use it."""
    if '/' not in reference:
        raise ValueError('Use the OpenCode form provider/model, for example mistral/devstral-latest')
    provider_id, model = reference.split('/', 1)
    configured = _user_providers().get(provider_id) or {}
    catalogue = _read(opencode_models_file()).get(provider_id) or {}
    if not configured and not catalogue:
        raise ValueError(f'OpenCode does not know provider {provider_id!r}; '
                         'configure it in OpenCode first or pass --base-url')
    options = configured.get('options') or {}
    base_url = options.get('baseURL') or catalogue.get('api')
    if not base_url:
        raise ValueError(f'No base URL is configured for {provider_id!r}; pass --base-url')
    settings = {**((catalogue.get('models') or {}).get(model) or {}), **((configured.get('models') or {}).get(model) or {})}
    if not settings and (configured.get('models') or catalogue.get('models')):
        known = sorted({*(configured.get('models') or {}), *(catalogue.get('models') or {})})
        raise ValueError(f'Unknown model {model!r} for {provider_id!r}. Known: {", ".join(known[:40])}')
    key_env = _env_reference(options.get('apiKey')) or next(iter(catalogue.get('env') or []), None)
    keep = ('name', 'reasoning', 'tool_call', 'attachment', 'temperature', 'limit', 'cost', 'options',
            'modalities', 'variants', 'headers')
    described = describe(base_url, model, npm=configured.get('npm') or catalogue.get('npm') or DEFAULT_NPM,
                         key_env=key_env, opencode_auth=provider_id,
                         model_settings={k: v for k, v in settings.items() if k in keep},
                         source='opencode:' + reference)
    # Keep the user's provider options (e.g. prompt-cache keys); the endpoint and key are the harness's.
    described['provider_options'] = {k: v for k, v in options.items() if k not in ('baseURL', 'apiKey')}
    return described


def describe(base_url, model, npm=DEFAULT_NPM, key_env=None, opencode_auth=None, model_settings=None,
             context=None, output=None, source='explicit'):
    base_url = (base_url or '').rstrip('/')
    if not re.match(r'https://[^/\s]+|http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)', base_url):
        raise ValueError('base URL must be https://, or http:// on this computer (a local model server)')
    if not model or not model.strip():
        raise ValueError('model id required')
    settings = dict(model_settings or {})
    settings.setdefault('name', model)
    settings.setdefault('tool_call', True)
    limit = dict(settings.get('limit') or DEFAULT_LIMIT)
    if context:
        limit['context'] = int(context)
    if output:
        limit['output'] = int(output)
    settings['limit'] = {'context': int(limit['context']), 'output': int(limit['output'])}
    return {'base_url': base_url, 'npm': npm, 'model': model, 'model_settings': settings,
            'key_env': key_env, 'opencode_auth': opencode_auth, 'key_source': None, 'model_source': source}


def key_candidates(settings):
    """(source, key) pairs: the environment variable, the user's OpenCode login, a literal config key."""
    candidates = []
    if settings.get('key_env') and os.environ.get(settings['key_env'], '').strip():
        candidates.append(('env:' + settings['key_env'], os.environ[settings['key_env']].strip()))
    provider_id = settings.get('opencode_auth')
    if provider_id:
        entry = _read(opencode_auth_file()).get(provider_id)
        if isinstance(entry, dict) and entry.get('type') == 'api' and entry.get('key'):
            candidates.append(('opencode-auth:' + provider_id, entry['key']))
        literal = ((_user_providers().get(provider_id) or {}).get('options') or {}).get('apiKey')
        if isinstance(literal, str) and literal and not literal.startswith('{'):
            candidates.append(('opencode-config:' + provider_id, literal))
    return candidates


def check_key(base_url, key):
    """True if the endpoint accepts the key (or needs none); None if it has no model list to ask."""
    request = Request(base_url + '/models', headers={'Authorization': 'Bearer ' + key} if key else {})
    try:
        with urlopen(request, timeout=15) as response:
            return response.status == 200
    except HTTPError as error:
        error.close()
        return False if error.code in (401, 403) else None
    except (URLError, OSError):
        return None


def choose_key_source(settings):
    """Remember where the working key lives, never the key itself."""
    candidates = key_candidates(settings)
    for source, key in candidates:
        if check_key(settings['base_url'], key) is not False:
            return source
    if not candidates and check_key(settings['base_url'], '') is not False:
        return 'none'
    tried = ', '.join(source for source, _ in candidates) or 'no key found'
    raise RuntimeError(f'{settings["base_url"]} did not accept a key (tried: {tried}). '
                       'Set the key in an environment variable (--key-env) or log in with `opencode auth login`.')


def resolve_key(settings):
    if settings.get('key_source') == 'none':
        return ''
    candidates = dict(key_candidates(settings))
    if settings.get('key_source') in candidates:
        return candidates[settings['key_source']]
    if candidates:
        return next(iter(candidates.values()))
    raise RuntimeError('No API key available for this agent; see `harness agent show`')
