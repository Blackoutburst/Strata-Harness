"""Run one Board ticket with one agent: checkout, OpenCode run, publish, report.

The agent works on its own clone and branch (agent/<name>/<number>-<slug>). The
harness, not the model, commits leftovers, pushes the branch and reports to the
Board; the manager reviews and merges.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

from .agents import write_private
from .boardlink import BoardError
from .providers import resolve_key
from .proxy import RunProxy
from .sandbox import engine_env, write_engine_config

RENEW_SECONDS = 600


ARTIFACTS = ('__pycache__/', '*.py[cod]', 'node_modules/', '.gradle/', 'build/', 'dist/', 'target/', '.venv/',
             '.DS_Store', '*.log', '.idea/', '.pytest_cache/', '.opencode/')


def git(workspace, *args, check=True, timeout=600, env=None):
    result = subprocess.run(['git', *args], cwd=workspace, capture_output=True, text=True, timeout=timeout,
                            env=env)
    if check and result.returncode:
        raise RuntimeError(f'git {" ".join(args)} failed: {result.stderr.strip()[-800:]}')
    return result.stdout.strip()


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:40] or 'task'


def prepare_checkout(agent, project, ticket):
    repository = (project.get('repository_url') or '').strip()
    if not repository:
        raise RuntimeError('The project has no repository; set one on the Board project')
    workspace = agent.dir / 'work' / ticket['id'][:12]
    branch = f'agent/{agent.name}/{ticket.get("number", 0)}-{slug(ticket["title"])}'
    if not (workspace / '.git').is_dir():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            shutil.rmtree(workspace)
        subprocess.run(['git', 'clone', '--quiet', repository, str(workspace)], check=True,
                       capture_output=True, text=True, timeout=1800)
    git(workspace, 'fetch', '--quiet', 'origin')
    default = git(workspace, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD', check=False) or 'origin/main'
    if git(workspace, 'rev-parse', '--abbrev-ref', 'HEAD') != branch:
        remote = git(workspace, 'ls-remote', '--heads', 'origin', branch, check=False)
        start = 'origin/' + branch if remote else default
        git(workspace, 'checkout', '--quiet', '-B', branch, start)
    if (workspace / '.gitmodules').exists():
        git(workspace, 'submodule', 'update', '--init', '--recursive', '--quiet', timeout=1800)
    # Local only (never committed): keep build output out of the agent's commits.
    exclude = workspace / '.git' / 'info' / 'exclude'
    exclude.parent.mkdir(exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ''
    missing = [pattern for pattern in ARTIFACTS if pattern not in existing.splitlines()]
    if missing:
        exclude.write_text(existing.rstrip('\n') + '\n# harness: build artifacts\n' + '\n'.join(missing) + '\n')
    return workspace, branch, default


def thread_feedback(session, ticket, limit=10):
    try:
        thread = session.tool('get_thread', thread_id=ticket['thread_id'], view='full')
    except BoardError:
        return []
    messages = thread.get('messages') or []
    return [f'- {m.get("author", {}).get("display_name") or m.get("author", {}).get("agent_name") or "someone"}: '
            f'{(m.get("body") or "").strip()[:4000]}' for m in messages[-limit:] if (m.get('body') or '').strip()]


def build_prompt(agent, project, ticket, branch, feedback, base='origin/main'):
    parts = [
        f'You are {agent.config.get("display_name") or agent.name}, a developer agent working on one task from '
        'the project board. A manager reviews and merges your work.',
        f'You work in this checkout (the current directory) on branch `{branch}`. The manager merges it into '
        f'`{base}`; if asked to update your branch, `git fetch origin` and rebase on `{base}`.',
        f'\n# Task #{ticket.get("number", "")}: {ticket["title"]}\n\n{ticket.get("description") or "(no description)"}',
    ]
    if ticket.get('acceptance_criteria'):
        parts.append('\n## Acceptance criteria\n\n' + ticket['acceptance_criteria'])
    project_text = '\n\n'.join(filter(None, [project.get('description'), project.get('context'),
                                              project.get('environment')]))
    if project_text:
        parts.append(f'\n## Project: {project.get("name", "")}\n\n{project_text}')
    if feedback:
        parts.append('\n## Discussion on this task (oldest first; includes manager review feedback)\n\n' + '\n'.join(feedback))
    if agent.config.get('instructions'):
        parts.append('\n## Your standing instructions\n\n' + agent.config['instructions'])
    if agent.config.get('tools'):
        parts.append('''
## Build and test tools

- `harness-tool build` runs `./gradlew assembleDebug assembleDebugAndroidTest testDebugUnitTest` and prints a digest.
- `harness-tool test` builds and runs the full instrumented suite on your own emulator (a snapshot is restored
  afterwards). `harness-tool test denied` runs the tests annotated as needing denied permissions, and
  `harness-tool test class:<fully.qualified.Class>` runs one class. The digest shows failures and this run's logcat.
- `harness-tool logs <job_id>:<page>` pages through a full log. Direct adb, emulator and Gradle device tasks are refused.
Run the tests yourself and iterate on real failures before you finish.''')
    for playbook in agent.config.get('playbooks') or []:
        try:
            parts.append('\n## Playbook\n\n' + Path(playbook).expanduser().read_text())
        except OSError:
            pass
    parts.append('''
## Rules

- Implement the task, then build and test it with the project's own tooling when it has any.
- Commit your work with clear messages (`git add`, `git commit`). Do not push, change remotes or switch
  branches: the harness publishes your branch for review.
- Never weaken, skip or delete tests to make them pass.
- If you are blocked (missing information, access, hardware), stop and say exactly what you need.
- Finish with a short report: what changed, how you verified it, what is left open.
  Its first line must be `RESULT: done` or `RESULT: blocked`.''')
    return '\n'.join(parts)


def read_events(path):
    texts, tokens, cost, errors = [], {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0}, 0.0, []
    finish = ''
    for line in path.read_text(errors='replace').splitlines() if path.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get('part') or {}
        if event.get('type') == 'text' and part.get('text'):
            texts.append(part['text'])
        elif event.get('type') == 'step_finish':
            used = part.get('tokens') or {}
            for name in ('input', 'output', 'reasoning'):
                tokens[name] += int(used.get(name) or 0)
            tokens['cache_read'] += int((used.get('cache') or {}).get('read') or 0)
            cost += float(part.get('cost') or 0)
            finish = part.get('reason') or ''
        elif event.get('type') == 'error':
            errors.append(((event.get('error') or {}).get('data') or {}).get('message') or 'error')
    return {'report': texts[-1].strip() if texts else '', 'tokens': tokens, 'cost_usd': round(cost, 4),
            'errors': errors, 'finish': finish}


TRUNCATED = ('Your previous reply hit the output token limit and was cut off before it finished, so nothing in '
             'it was applied. Continue the task from where you were. Write large files in several smaller '
             'edits (a few hundred lines each), not in one reply.')


OUTAGE = re.compile(r'service unavailable|overloaded|rate.?limit|too many requests|bad gateway|gateway timeout|'
                    r'temporarily unavailable|internal server error', re.IGNORECASE)


def provider_outage(errors):
    return bool(errors) and all(OUTAGE.search(error or '') for error in errors)


def cold_start_failure(code, timed_out, events):
    """OpenCode failed before the model produced anything (e.g. provider package install)."""
    return not timed_out and code != 0 and not any(events['tokens'].values())


def stop_process(process):
    if os.name == 'nt':
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def run_opencode(agent, workspace, prompt, run_dir, deadline_seconds, session_id=None):
    key = resolve_key(agent.config)
    proxy = RunProxy(agent.config['base_url'], key, run_dir / 'proxy.jsonl')
    proxy_url = proxy.start()
    tools = None
    try:
        model = write_engine_config(agent, proxy_url, proxy.token)
        env = engine_env(agent)
        if agent.config.get('tools'):
            from .tools import LocalTools
            tools = LocalTools(agent, workspace, run_dir, dict(env))
            env.update(tools.start(run_dir / 'bin'))
            env['PATH'] = str(run_dir / 'bin') + os.pathsep + env.get('PATH', '')
        command = [agent.config.get('opencode') or shutil.which('opencode') or 'opencode', 'run', '--format', 'json',
                   '--auto', '--model', model, '--dir', str(workspace), '--title', run_dir.name]
        if session_id:
            command += ['--session', session_id]
        command.append(prompt)
        with open(run_dir / 'events.jsonl', 'w') as events, open(run_dir / 'opencode.log', 'w') as log:
            process = subprocess.Popen(command, cwd=workspace, env=env, stdout=events, stderr=log,
                                       start_new_session=(os.name != 'nt'))
            try:
                return process.wait(timeout=deadline_seconds), False
            except subprocess.TimeoutExpired:
                stop_process(process)
                return process.returncode, True
            except BaseException:
                # The harness is stopping: never leave an orphaned OpenCode behind.
                stop_process(process)
                raise
    finally:
        if tools:
            tools.stop()
        proxy.close()


def session_of(path):
    """The OpenCode session a run used, from its events."""
    for line in path.read_text(errors='replace').splitlines() if path.exists() else []:
        try:
            value = json.loads(line).get('sessionID')
        except ValueError:
            continue
        if value:
            return value
    return None


class Renewer(threading.Thread):
    """Keeps the Board claim alive while the agent works."""

    def __init__(self, session, ticket, log=print):
        super().__init__(daemon=True)
        self.session, self.ticket, self.stop, self.log = session, ticket, threading.Event(), log
        self.failures = 0

    def renew(self):
        renewed = self.session.tool('renew_ticket', ticket_id=self.ticket['id'],
                                    expected_version=self.ticket['version'], progress='Agent is working', view='full')
        self.ticket = renewed.get('ticket', renewed) if 'version' not in renewed else renewed

    def run(self):
        while not self.stop.wait(RENEW_SECONDS):
            try:
                try:
                    self.renew()
                except BoardError:
                    # Usually a version change (a message, an edit): reread and renew once more.
                    current = self.session.tool('get_ticket', ticket_id=self.ticket['id'], view='full')
                    self.ticket = current.get('ticket', current)
                    self.renew()
                self.failures = 0
            except Exception as error:  # Never let the claim lapse silently.
                self.failures += 1
                self.log(f'claim renewal failed ({self.failures}): {type(error).__name__}: {str(error)[:300]}')


def run_ticket(agent, session, ticket, log=print):
    """Ticket must already be claimed by this session. Returns the run result."""
    project = session.tool('get_project', project_id=ticket['project_id'], view='full')
    project = project.get('project', project)
    run_dir = agent.dir / 'runs' / (time.strftime('%Y%m%d-%H%M%S-') + ticket['id'][:8])
    run_dir.mkdir(parents=True, mode=0o700)
    renewer = Renewer(session, ticket, log)
    renewer.start()
    result = {'ticket_id': ticket['id'], 'agent': agent.name, 'started_at': time.time()}
    try:
        workspace, branch, default = prepare_checkout(agent, project, ticket)
        base = git(workspace, 'rev-parse', 'HEAD')
        sessions_path = agent.dir / 'sessions.json'
        sessions = json.loads(sessions_path.read_text()) if sessions_path.exists() else {}
        previous = sessions.get(ticket['id']) if agent.config.get('continue_sessions', True) else None
        feedback = thread_feedback(session, ticket)
        if previous:
            # Same OpenCode session: the agent keeps its context; send only what is new.
            prompt = ('## New discussion on this task (latest last)\n\n' + '\n'.join(feedback[-6:]) +
                      '\n\nContinue the same task in this checkout on branch `' + branch + '`. Same rules as before; '
                      'finish with a report whose first line is `RESULT: done` or `RESULT: blocked`.')
        else:
            prompt = build_prompt(agent, project, ticket, branch, feedback, default)
        (run_dir / 'prompt.md').write_text(prompt)
        log(f'[{agent.name}] ticket #{ticket.get("number")} {ticket["title"]!r} on {branch}')
        deadline = int(agent.config.get('max_minutes', 60)) * 60
        code, timed_out = run_opencode(agent, workspace, prompt, run_dir, deadline, previous)
        events = read_events(run_dir / 'events.jsonl')
        if previous and code != 0 and not any(events['tokens'].values()):
            # The saved session is gone (engine data reset): start fresh with the full brief.
            previous = None
            prompt = build_prompt(agent, project, ticket, branch, feedback, default)
            code, timed_out = run_opencode(agent, workspace, prompt, run_dir, deadline)
            events = read_events(run_dir / 'events.jsonl')
        for attempt in range(4):
            # The engine stopped on a provider/stream error, or a reply was cut off at the output limit: continue the same session.
            current = session_of(run_dir / 'events.jsonl')
            remaining = deadline - (time.time() - result['started_at'])
            truncated = events['finish'] == 'length'
            failed = code != 0 and events['errors']
            if timed_out or not (failed or truncated) or not current or remaining < 300:
                break
            if truncated:
                log(f'[{agent.name}] reply cut off at the output token limit; continuing the session')
                nudge = TRUNCATED
            else:
                log(f'[{agent.name}] engine stopped on "{events["errors"][-1][:80]}"; continuing the session')
                nudge = 'The previous model request failed with a provider error. Continue the task from where you were.'
            (run_dir / f'events-{attempt}.jsonl').write_bytes((run_dir / 'events.jsonl').read_bytes())
            code, timed_out = run_opencode(agent, workspace, nudge, run_dir, int(remaining), current)
            merged = read_events(run_dir / 'events.jsonl')
            for name, value in merged['tokens'].items():
                events['tokens'][name] += value
            events['cost_usd'] = round(events['cost_usd'] + merged['cost_usd'], 4)
            events['report'], events['errors'] = merged['report'] or events['report'], merged['errors']
            events['finish'] = merged['finish']
        if session_of(run_dir / 'events.jsonl'):
            sessions[ticket['id']] = session_of(run_dir / 'events.jsonl')
            write_private(sessions_path, sessions)
        if cold_start_failure(code, timed_out, events):
            # A first-run provider package install can be left half-written; reinstall once.
            shutil.rmtree(agent.dir / 'engine' / 'cache' / 'opencode' / 'packages', ignore_errors=True)
            code, timed_out = run_opencode(agent, workspace, prompt, run_dir, deadline)
            events = read_events(run_dir / 'events.jsonl')
        # Deliver what the agent left uncommitted (build output is excluded), and say so in the report.
        leftovers = git(workspace, 'status', '--porcelain').splitlines()
        if leftovers:
            git(workspace, 'add', '-A')
            git(workspace, 'commit', '--quiet', '-m', f'{agent.name}: uncommitted changes at end of run',
                env=engine_env(agent))
        untracked = [line[3:] for line in leftovers]
        new_commits = int(git(workspace, 'rev-list', '--count', f'{base}..HEAD'))
        ahead = int(git(workspace, 'rev-list', '--count', f'{default}..HEAD'))
        pushed = False
        if ahead and agent.config.get('push', True):
            git(workspace, 'push', '--quiet', '--force-with-lease', 'origin', f'HEAD:refs/heads/{branch}')
            pushed = True
        report = events['report']
        blocked = timed_out or code != 0 or ahead == 0 or report.upper().startswith('RESULT: BLOCKED')
        status = 'blocked' if blocked else 'review'
        if code != 0 and not timed_out and new_commits == 0 and provider_outage(events['errors']):
            status = 'retry'  # The provider failed, not the agent: hand the ticket back and try later.
        result.update(branch=branch, workspace=str(workspace), exit_code=code, timed_out=timed_out,
                      untracked=untracked[:50],
                      new_commits=new_commits, commits_ahead=ahead, pushed=pushed, report=report,
                      tokens=events['tokens'], cost_usd=events['cost_usd'], errors=events['errors'][-3:],
                      status=status)
    except Exception as error:  # Report every failure on the ticket rather than leaving it claimed.
        result.update(status='blocked', error=f'{type(error).__name__}: {error}'[:2000])
    except BaseException:
        # Stopped (shutdown, Ctrl-C): give the ticket back so the agent resumes it on its branch later.
        renewer.stop.set()
        try:
            current = session.tool('get_ticket', ticket_id=ticket['id'], view='full')
            current = current.get('ticket', current)
            session.tool('release_ticket', ticket_id=current['id'], expected_version=current['version'],
                         idempotency_key=f'stop-{run_dir.name}', reason='Harness stopped during the run',
                         status='todo')
        except Exception:
            pass
        raise
    finally:
        renewer.stop.set()
        renewer.join(timeout=5)
    result['finished_at'] = time.time()
    write_private(run_dir / 'result.json', result)
    report_to_board(session, renewer.ticket, result)
    log(f'[{agent.name}] ticket #{ticket.get("number")} -> {result["status"]} '
        f'({result.get("commits_ahead", 0)} commits, ${result.get("cost_usd", 0)})')
    return result


def report_to_board(session, ticket, result):
    lines = [f'**{result["agent"]}** finished: **{result["status"]}**']
    if result.get('branch'):
        where = 'pushed to origin' if result.get('pushed') else f'local checkout `{result.get("workspace")}`'
        lines.append(f'Branch `{result["branch"]}` ({result.get("commits_ahead", 0)} commits ahead, {where})')
    if result.get('tokens'):
        t = result['tokens']
        lines.append(f'Tokens in {t["input"]} / out {t["output"]} / cached {t["cache_read"]}; cost ${result["cost_usd"]}')
    if result.get('timed_out'):
        lines.append('Stopped at the time limit.')
    if result.get('untracked'):
        lines.append('Committed by the harness (left uncommitted by the agent): ' + ', '.join(result['untracked'][:20]))
    if result.get('error') or result.get('errors'):
        lines.append('Errors: ' + '; '.join(filter(None, [result.get('error'), *result.get('errors', [])]))[:1500])
    summary = '\n'.join(lines)
    report = result.get('report') or '(no report from the agent)'
    current = session.tool('get_ticket', ticket_id=ticket['id'], view='full')
    current = current.get('ticket', current)
    try:
        session.tool('post_message', thread_id=current['thread_id'], body=summary + '\n\n' + report[:15000],
                     idempotency_key='run-' + str(int(result['started_at'])) + '-' + ticket['id'][:8])
    except BoardError:
        pass
    if result['status'] == 'retry':
        session.tool('release_ticket', ticket_id=current['id'], expected_version=current['version'],
                     idempotency_key='retry-' + str(int(result['started_at'])) + '-' + ticket['id'][:8],
                     reason='Model provider unavailable; the agent will retry later', status='todo')
        return
    changes = {'progress': summary[:20000], 'release_claim': True}
    if result['status'] == 'review':
        changes.update(status='review', evidence=summary[:20000])
    else:
        changes.update(status='blocked', blocked_reason=(result.get('error') or report)[:2000] or 'Agent run failed')
    session.tool('update_ticket', ticket_id=current['id'], expected_version=current['version'], changes=changes,
                 view='full')


def smoke_test(agent):
    """One tiny real run through the agent's sandbox, proxy and provider."""
    import tempfile
    run_dir = agent.dir / 'runs' / ('check-' + time.strftime('%Y%m%d-%H%M%S'))
    run_dir.mkdir(parents=True, mode=0o700)
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(['git', 'init', '--quiet', directory], check=True)
        for attempt in range(2):
            code, timed_out = run_opencode(agent, Path(directory), 'Reply with the single word OK.', run_dir, 300)
            events = read_events(run_dir / 'events.jsonl')
            if not cold_start_failure(code, timed_out, events):
                break
            shutil.rmtree(agent.dir / 'engine' / 'cache' / 'opencode' / 'packages', ignore_errors=True)
    ok = code == 0 and not timed_out and 'OK' in events['report'].upper()
    return ok, events, run_dir
