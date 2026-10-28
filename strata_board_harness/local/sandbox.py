"""One OpenCode installation per agent, so concurrent agents never share state.

Each agent gets its own HOME and XDG config/data/cache/state directories, hence its
own opencode.db, sessions, logs and provider packages. The model endpoint is the
run's loopback proxy; the real provider key never enters this environment.
This isolates agents from each other; it is not an OS security sandbox.
"""

import json
import os
from pathlib import Path
import re
import shutil

PLUGINS = Path(__file__).resolve().parents[2] / 'plugins'
SECRET = re.compile(r'KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION', re.IGNORECASE)
PROVIDER_ID = 'harness'


def engine_env(agent, base_env=None):
    root = agent.dir / 'engine'
    env = {key: value for key, value in (base_env or os.environ).items()
           if not SECRET.search(key) and not key.startswith(('XDG_', 'OPENCODE_')) and key not in ('HOME', 'USERPROFILE')}
    env.update(agent.config.get('env') or {})
    env['HOME'] = str(root / 'home')
    if os.name == 'nt':
        env['USERPROFILE'] = env['HOME']
    for name in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
        env['XDG_' + name + '_HOME'] = str(root / name.lower())
    env.update(OPENCODE_DISABLE_AUTOUPDATE='1', OPENCODE_DISABLE_PROJECT_CONFIG='1',
               OPENCODE_DISABLE_MODELS_FETCH='1', OPENCODE_DISABLE_LSP_DOWNLOAD='1',
               OPENCODE_DISABLE_DEFAULT_PLUGINS='1')
    identity = agent.config.get('display_name') or agent.name
    env.update(GIT_AUTHOR_NAME=identity, GIT_COMMITTER_NAME=identity,
               GIT_AUTHOR_EMAIL=f'{agent.name}@agents.local', GIT_COMMITTER_EMAIL=f'{agent.name}@agents.local',
               GIT_TERMINAL_PROMPT='0')
    return env


def write_engine_config(agent, proxy_url, proxy_token):
    """The agent's opencode.json: one provider (the proxy), one model, the guard plugin."""
    root = agent.dir / 'engine'
    config_dir = root / 'config' / 'opencode'
    (config_dir / 'plugins').mkdir(parents=True, exist_ok=True)
    for name in ('home', 'data', 'state', 'cache'):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGINS / 'workspace_guard.ts', config_dir / 'plugins' / 'workspace_guard.ts')
    home_config = root / 'home' / '.config'
    if not home_config.exists() and os.name != 'nt':
        home_config.symlink_to(root / 'config', target_is_directory=True)
    model = agent.config['model']
    settings = {
        '$schema': 'https://opencode.ai/config.json',
        'autoupdate': False, 'share': 'disabled', 'mcp': {},
        'enabled_providers': [PROVIDER_ID],
        'model': f'{PROVIDER_ID}/{model}', 'small_model': f'{PROVIDER_ID}/{model}',
        'provider': {PROVIDER_ID: {
            'name': agent.config.get('display_name') or agent.name, 'npm': agent.config['npm'],
            'options': {'baseURL': proxy_url, 'apiKey': proxy_token},
            'models': {model: agent.config['model_settings']}}},
        'permission': {'edit': 'allow', 'bash': 'allow', 'webfetch': 'allow'},
    }
    path = config_dir / 'opencode.json'
    path.write_text(json.dumps(settings, indent=2))
    path.chmod(0o600)
    return f'{PROVIDER_ID}/{model}'
