"""Keep agents working: each agent polls the Board for tickets assigned to it.

`serve_agent` runs one agent (one ticket at a time, guarded by the agent's lock).
`serve_all` supervises one `serve_agent` process per agent and picks up new agents.
"""

import os
import signal
from pathlib import Path
import subprocess
import sys
import time

from .agents import Agent
from .boardlink import AgentSession, BoardError
from .runner import run_ticket

OUTAGE_BACKOFF = 600  # seconds to wait after the model provider failed
PRIORITY = {'urgent': 0, 'high': 1, 'normal': 2, 'low': 3}


class AgentLock:
    """Held for as long as an agent process runs; released by the OS if it dies."""

    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def __enter__(self):
        self.stream = open(self.path, 'a+')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise SystemExit(f'Agent already running (lock {self.path})') from None
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(str(os.getpid()))
        self.stream.flush()
        return self

    def __exit__(self, *_):
        self.stream.close()

    @staticmethod
    def held(path):
        try:
            with AgentLock(path):
                return False
        except SystemExit:
            return True


def connect(agent):
    board = agent.config['board']
    return AgentSession(agent.credential, board['identity_key'], agent.config.get('display_name') or agent.name,
                        label=f'{agent.name} (harness)', autonomous=True)


def next_ticket(agent, session):
    work = session.tool('get_work', view='full')
    projects = agent.config['board'].get('project_ids')
    # The board lets work start once a dependency is in review; an agent waits until it is merged
    # (done), because its checkout starts from the main branch.
    mine = [t for t in work.get('ready') or [] if t.get('assignee_agent_id') == session.profile_id
            and (not projects or t.get('project_id') in projects)
            and all(d.get('status') == 'done' for d in t.get('dependencies') or [])]
    if mine:
        mine.sort(key=lambda t: (PRIORITY.get(t.get('priority'), 2), t.get('created_at', 0)))
        ticket = mine[0]
        return session.tool('claim_ticket', ticket_id=ticket['id'], expected_version=ticket['version'], view='full')
    if agent.config.get('take_unassigned'):
        selected = session.tool('claim_next_ticket', idempotency_key=f'{agent.name}-{int(time.time())}', view='full')
        if selected.get('claimed'):
            return selected['ticket']
    return None


def serve_agent(agent, once=False, poll_seconds=30, log=print):
    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    with AgentLock(agent.dir / 'lock'):
        log(f'[{agent.name}] serving {agent.config["board"]["url"]} with {agent.config["model"]}')
        session, backoff = None, poll_seconds
        while True:
            try:
                session = session or connect(agent)
                ticket = next_ticket(agent, session)
                backoff = poll_seconds
                if ticket:
                    result = run_ticket(agent, session, ticket, log=log)
                    if result.get('status') == 'retry':
                        backoff = OUTAGE_BACKOFF
                    else:
                        continue
            except BoardError as error:
                log(f'[{agent.name}] board: {error}')
                session, backoff = None, min(backoff * 2, 600)
            if once:
                return
            time.sleep(backoff)


def serve_all(poll_seconds=30, log=print):
    """One child process per agent; restarts crashed agents and starts new ones."""
    children = {}

    def stop(*_):
        # Each agent hands its ticket back before exiting; wait so a restart finds the locks free.
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                child.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, stop)
    while True:
        for agent in Agent.all():
            child = children.get(agent.name)
            if agent.config.get('enabled', True) and (child is None or child.poll() is not None):
                if child is not None:
                    log(f'[{agent.name}] exited with {child.returncode}; restarting')
                with open(agent.dir / 'serve.log', 'a') as output:
                    children[agent.name] = subprocess.Popen(
                        [sys.executable, '-u', '-m', 'strata_board_harness.local', 'serve', '--agent', agent.name,
                         '--poll', str(poll_seconds)], stdout=output, stderr=subprocess.STDOUT,
                        cwd=Path(__file__).resolve().parents[2])
        time.sleep(poll_seconds)
