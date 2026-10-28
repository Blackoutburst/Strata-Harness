"""harness: permanent local coding agents that take work from an Agent Board.

    harness agent add glm --model mistral/zai-glm-5-3-flash     # any provider/model your OpenCode knows
    harness agent add local --base-url http://127.0.0.1:8080/v1 --model qwen3-coder   # any compatible server
    harness agent list | show NAME | remove NAME
    harness serve                      # run every agent (what autostart runs)
    harness run NAME --ticket ID       # one ticket now, in the foreground
    harness autostart install          # start agents at login (macOS, Linux, Windows)
    harness manager grant              # let the Claude Code manager accept/return reviewed tickets
    harness doctor
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import autostart
from .agents import Agent, harness_home
from .boardlink import BoardError, LocalOwner, board_url
from .daemon import AgentLock, connect, serve_agent, serve_all
from .providers import DEFAULT_NPM, choose_key_source, describe, from_opencode
from .runner import run_ticket, smoke_test

DEFAULT_BOARD = 'http://127.0.0.1:9140'


def text_or_file(value):
    if value and value.startswith('@'):
        return Path(value[1:]).expanduser().read_text()
    return value or ''


def agent_add(args):
    if args.base_url:
        model = describe(args.base_url, args.model, npm=args.npm or DEFAULT_NPM, key_env=args.key_env,
                         context=args.context, output=args.output)
    else:
        model = from_opencode(args.model)
        if args.context or args.output:
            model = describe(model['base_url'], model['model'], model['npm'], model['key_env'],
                             model['opencode_auth'], model['model_settings'], args.context, args.output,
                             model['model_source'])
        if args.key_env:
            model['key_env'] = args.key_env
    model['key_source'] = choose_key_source(model)
    owner = LocalOwner(board_url(args.board))
    agent = Agent.create(args.name, args.display_name or args.name, model, owner.url, args.max_minutes,
                         not args.no_push, args.project or None, text_or_file(args.instructions))
    try:
        agent.config['take_unassigned'] = args.take_unassigned
        owner.enable_agents()
        from .agents import write_private
        write_private(agent.dir / 'board.private.json', owner.pair(agent.config['display_name']))
        session = connect(agent)
        owner.grant_worker(session.profile_id, args.project or None)
        agent.config['board']['profile_id'] = session.profile_id
        agent.save()
        print(f'Agent {agent.name}: board profile {session.profile_id}, model {model["model"]} at {model["base_url"]}'
              f' (key: {model["key_source"]})')
        if not args.skip_check:
            print('Checking the model through the agent sandbox ...', flush=True)
            ok, events, run_dir = smoke_test(agent)
            if not ok:
                raise RuntimeError(f'Model check failed: {events["errors"] or events["report"][:300]} (see {run_dir})')
            print(f'Model answered ({events["tokens"]["input"]} input tokens, ${events["cost_usd"]}).')
    except Exception:
        print(f'Setup failed; removing {agent.dir}. The board device stays listed in Settings.', file=sys.stderr)
        agent.remove()
        raise
    print('Assign tickets to it on the board; `harness serve` (or autostart) will pick them up.')


def agent_list(args):
    for agent in Agent.all():
        running = AgentLock.held(agent.dir / 'lock')
        last = sorted((agent.dir / 'runs').glob('*/result.json'))[-1:] if (agent.dir / 'runs').is_dir() else []
        status = json.loads(last[0].read_text()).get('status') if last else '-'
        print(f'{agent.name:16} {agent.config["model"]:32} {"running" if running else "idle":8} last={status}')


def agent_show(args):
    agent = Agent.load(args.name)
    print(json.dumps({**agent.config, 'directory': str(agent.dir)}, indent=2))


def agent_remove(args):
    agent = Agent.load(args.name)
    if AgentLock.held(agent.dir / 'lock'):
        raise SystemExit('Agent is running; stop `harness serve` first')
    agent.remove()
    print(f'Removed {args.name}. Revoke its device in the board Settings if you no longer need it.')


def run_once(args):
    agent = Agent.load(args.name)
    with AgentLock(agent.dir / 'lock'):
        session = connect(agent)
        ticket = session.tool('get_ticket', ticket_id=args.ticket, view='full')
        ticket = ticket.get('ticket', ticket)
        ticket = session.tool('claim_ticket', ticket_id=ticket['id'], expected_version=ticket['version'], view='full')
        result = run_ticket(agent, session, ticket)
    print(json.dumps({k: v for k, v in result.items() if k != 'report'}, indent=2))


def serve(args):
    if args.agent:
        serve_agent(Agent.load(args.agent), once=args.once, poll_seconds=args.poll)
    else:
        serve_all(poll_seconds=args.poll)


def manager_grant(args):
    owner = LocalOwner(board_url(args.board))
    profiles = owner.profiles()
    harness_ids = {agent.config['board'].get('profile_id') for agent in Agent.all()}
    if args.name:
        matches = [p for p in profiles if p.get('name') == args.name or p.get('id') == args.name]
    else:
        matches = [p for p in profiles if (p.get('name') or '').startswith('Claude Code') and p['id'] not in harness_ids]
    if len(matches) != 1:
        names = ', '.join(repr(p.get('name')) for p in profiles if p['id'] not in harness_ids) or 'none'
        raise SystemExit(f'Name the manager agent exactly (agent profiles on the board: {names})')
    profile = owner.grant_review(matches[0]['id'])
    print(f'{profile.get("name")} may now accept or return tickets in review. '
          'It must register again (register_agent) for the new permission to apply.')


def doctor(args):
    ok = True
    opencode = shutil.which('opencode')
    version = subprocess.run([opencode, '--version'], capture_output=True, text=True).stdout.strip() if opencode else ''
    print(f'opencode: {opencode or "NOT FOUND (install from https://opencode.ai)"} {version}')
    ok &= bool(opencode)
    print(f'git: {shutil.which("git") or "NOT FOUND"}')
    ok &= bool(shutil.which('git'))
    print(f'harness home: {harness_home()}')
    try:
        owner = LocalOwner(args.board)
        print(f'board: {owner.url} reachable, owner {owner.owner_id}, no login')
    except (BoardError, ValueError) as error:
        print(f'board: {error}')
        ok = False
    for agent in Agent.all():
        print(f'agent {agent.name}: {agent.config["model"]} via {agent.config["base_url"]}')
    raise SystemExit(0 if ok else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='harness', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    agents = commands.add_parser('agent', help='create and manage agents').add_subparsers(dest='action', required=True)
    add = agents.add_parser('add', help='create an agent, pair it with the board and check its model')
    add.add_argument('name', help='short id: lowercase letters, digits, "-"')
    add.add_argument('--model', required=True,
                     help='provider/model as OpenCode names it, or a raw model id together with --base-url')
    add.add_argument('--base-url', help='OpenAI-compatible (or --npm) endpoint, e.g. http://127.0.0.1:8080/v1')
    add.add_argument('--npm', help='AI SDK package for --base-url (default @ai-sdk/openai-compatible)')
    add.add_argument('--key-env', help='environment variable holding the API key')
    add.add_argument('--context', type=int, help='context window in tokens')
    add.add_argument('--output', type=int, help='maximum output tokens')
    add.add_argument('--display-name', help='name shown on the board')
    add.add_argument('--board', default=os.environ.get('BOARD_URL', DEFAULT_BOARD))
    add.add_argument('--project', action='append', help='limit to this board project id (repeatable)')
    add.add_argument('--max-minutes', type=int, default=60, help='time limit per ticket')
    add.add_argument('--instructions', help='standing instructions, or @file')
    add.add_argument('--take-unassigned', action='store_true', help='also take unassigned ready tickets')
    add.add_argument('--no-push', action='store_true', help='keep branches in the agent checkout only')
    add.add_argument('--skip-check', action='store_true', help='skip the live model check')
    add.set_defaults(func=agent_add)
    agents.add_parser('list').set_defaults(func=agent_list)
    show = agents.add_parser('show')
    show.add_argument('name')
    show.set_defaults(func=agent_show)
    remove = agents.add_parser('remove')
    remove.add_argument('name')
    remove.set_defaults(func=agent_remove)

    run = commands.add_parser('run', help='run one ticket with one agent now')
    run.add_argument('name')
    run.add_argument('--ticket', required=True)
    run.set_defaults(func=run_once)

    serving = commands.add_parser('serve', help='run agents: poll the board and work on assigned tickets')
    serving.add_argument('--agent', help='only this agent')
    serving.add_argument('--once', action='store_true', help='one polling pass (with --agent)')
    serving.add_argument('--poll', type=int, default=30, help='seconds between board checks')
    serving.set_defaults(func=serve)

    auto = commands.add_parser('autostart', help='start `harness serve` at login')
    auto.add_argument('action', choices=['install', 'uninstall'])
    auto.set_defaults(func=lambda a: print(autostart.install() if a.action == 'install'
                                           else autostart.uninstall() or 'Removed autostart.'))

    manager = commands.add_parser('manager', help='set up the manager (Claude Code) on the board')
    manager.add_argument('action', choices=['grant'], help='grant: let it accept or return reviewed tickets')
    manager.add_argument('name', nargs='?', help='its board name (default: the one Claude Code agent)')
    manager.add_argument('--board', default=os.environ.get('BOARD_URL', DEFAULT_BOARD))
    manager.set_defaults(func=manager_grant)

    check = commands.add_parser('doctor', help='check prerequisites')
    check.add_argument('--board', default=os.environ.get('BOARD_URL', DEFAULT_BOARD))
    check.set_defaults(func=doctor)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (BoardError, RuntimeError, ValueError) as error:
        raise SystemExit(f'error: {error}') from None
