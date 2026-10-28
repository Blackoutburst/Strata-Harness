"""Permanent local agents: one directory each, nothing shared between them.

    <home>/agents/<name>/
        agent.json            definition (model, board, limits)
        board.private.json    paired Board credential (mode 0600)
        engine/               the agent's own OpenCode HOME, config, data, cache, state
        work/<ticket>/        one Git checkout per ticket
        runs/<run>/           prompt, events, model-proxy usage, result
        lock                  held while the agent runs
"""

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time

NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')


def harness_home():
    if os.environ.get('HARNESS_HOME'):
        return Path(os.environ['HARNESS_HOME']).expanduser()
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'strata-harness'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'strata-harness'


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
    temp.replace(path)


class Agent:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.config = json.loads((self.dir / 'agent.json').read_text())
        self.name = self.config['name']

    @property
    def credential(self):
        return json.loads((self.dir / 'board.private.json').read_text())

    def save(self):
        write_private(self.dir / 'agent.json', self.config)

    @staticmethod
    def path(name):
        return harness_home() / 'agents' / name

    @classmethod
    def load(cls, name):
        directory = cls.path(name)
        if not (directory / 'agent.json').is_file():
            raise SystemExit(f'No agent named {name!r}; see `harness agent list`')
        return cls(directory)

    @classmethod
    def all(cls):
        root = harness_home() / 'agents'
        return [cls(path) for path in sorted(root.iterdir()) if (path / 'agent.json').is_file()] if root.is_dir() else []

    @classmethod
    def create(cls, name, display_name, provider, board_url, max_minutes=60, push=True, project_ids=None,
               instructions=''):
        if not NAME.fullmatch(name):
            raise ValueError('Agent names use lowercase letters, digits and "-" (max 32)')
        directory = cls.path(name)
        if directory.exists():
            raise ValueError(f'Agent {name!r} already exists at {directory}')
        directory.mkdir(parents=True, mode=0o700)
        config = {'name': name, 'display_name': display_name, **provider,
                  'board': {'url': board_url, 'identity_key': secrets.token_hex(16), 'profile_id': None,
                            'project_ids': project_ids},
                  'max_minutes': max_minutes, 'push': push, 'instructions': instructions,
                  'created_at': time.time()}
        write_private(directory / 'agent.json', config)
        return cls(directory)

    def remove(self):
        shutil.rmtree(self.dir)
