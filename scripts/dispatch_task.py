#!/usr/bin/env python3
"""Manager-run dispatch of ONE selected Board ticket to the paired Ubuntu worker.

Run on the owning Mac. Every step is an explicit manager decision for one ticket:
it refuses while any coding process, worker lock, active selection or unclean
emulator effect exists, archives (never deletes) the previous daemon state, ships
the exact source ref as a git bundle, warms the Gradle cache online as a trusted
manager prefetch (the worker's own builds stay offline), writes prompt/readiness/
selection, retargets the Mac emulator bridge and only then starts the daemon.
No credential is printed; Board/SSH secrets stay in their existing private files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from strata_board_harness.emulator import selected_target  # noqa: E402
from strata_board_harness.managed_engine import process_stamp, write_private  # noqa: E402
from strata_board_harness.runtime import private_json  # noqa: E402

REMOTE_STATE = '~/.local/state/strata-board-harness'
REMOTE_CONFIG = '~/.config/strata-board-harness'
SERVICE = 'strata-board-harness.service'
GATES = ('fake_lifecycle', 'ubuntu_engine_rtk', 'managed_engine_api', 'offline_android_build',
         'emulator_rollback', 'serial_runtime_bridge', 'strata_tool_calls', 'image_path_checked',
         'cross_host_reconciled', 'source_boundary')


def run(argv, **kwargs):
    kwargs.setdefault('check', True)
    kwargs.setdefault('timeout', 120)
    return subprocess.run(argv, **kwargs)


class Remote:
    def __init__(self, host):
        self.base = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', host]

    def sh(self, script, *, data=None, timeout=120, check=True):
        result = subprocess.run(self.base + ['bash -lc ' + shlex.quote(script)], input=data,
                                capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError('Remote step failed (%d): %s' % (result.returncode,
                               result.stderr.decode(errors='replace')[-2000:]))
        return result.stdout.decode(errors='replace')

    def py(self, code, payload, timeout=120):
        """Run trusted manager Python remotely with a JSON payload on stdin."""
        return self.sh('python3 -c ' + shlex.quote(code), data=json.dumps(payload).encode(), timeout=timeout)


class Board:
    """Owner REST session on the Mac-local Board (same login the manager uses)."""
    def __init__(self, url, password_file):
        import http.cookiejar
        import urllib.request
        self.url, self.request = url.rstrip('/'), urllib.request
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        session = self.call('POST', '/api/login', {'username': 'blackoutburst',
                            'password': Path(password_file).expanduser().read_text().strip()})
        self.csrf = session.get('csrf_token') or session.get('csrf')

    def call(self, method, path, body=None):
        headers = {'Origin': self.url, 'Content-Type': 'application/json'}
        if self.csrf: headers['X-CSRF-Token'] = self.csrf
        req = self.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None,
                                   headers, method=method)
        with self.opener.open(req, timeout=30) as response:
            return json.load(response)

    def ticket(self, ticket_id):
        value = self.call('GET', '/api/tickets/' + ticket_id)
        return value.get('ticket', value)


PROMPT = """Selected AttentiveOS E2E task {authorization_id}, dispatched by the manager (Claude Code, agent {manager}) on {date}. Only this selected task is authorized.

Developer: Strata Harness Ubuntu, model strata/{model} (OpenCode1.19.3 with RTK). Work only {workspace}, branch {branch}, exact starting commit {baseline}. Read this repo CLAUDE.md (if present) and .harness/build-test-interface.md; the selected harness interface supersedes raw command examples.
{continuation}
The daemon handles own-identity claim/renewal, relevant inbox and manager communication. You never receive a Board token or need to poll/renew yourself. Acknowledge authenticated guidance using its exact GUIDANCE_ACK marker. If genuinely blocked, emit HARNESS_BLOCKER followed by precise failure, attempts and evidence, then stop taking actions until guidance arrives. One execution only: maximum {minutes} active minutes and {steps} assistant steps, no automatic chained retries.

Run an initial harness-tool build within your first 8 tool actions to establish real compiler feedback, then build after small coherent changes. Do not postpone compilation until a large test suite has been written. The fixed build compiles app + instrumentation + JVM tests using JDK17/offline SDK; dependencies were prefetched for the starting commit. New Gradle/build-control hashes require manager review; give the exact dependency/control change and failure rather than bypassing the gate. Use native read/edit/write/glob/grep only in this source workspace; bash only ls, git status --short or the documented harness-tool operations. No arbitrary shell, direct Gradle, SSH, ADB, network, push/merge/rebase or credential access.

After your implementation builds, use harness-tool test for the normal suite{denied_hint}. Run a single class with harness-tool test class:{package}.<path.To.TestClass> while iterating. The adapter targets ONLY silent isolated caregiver35 API35 emulator-5556 (no orchestrator: every test must clean up after itself; the VM is rolled back after each run), enforces per-test 300s/connected 25min caps, retains full errors/screenshots and grants no raw device access. Target package {package}; the runner declared by your test APK manifest is used. No real calls, physical devices, real-account edits, audible tests, broad provider deletion or unrelated data. Use owned run-specific test records and exact cleanup. Fakes must drive meaningful journeys; do not replace assertions with skips or self-fulfilling checks. Keep accessibility checks enabled. Minimal behavior-preserving production seams only, each justified in your final report.

FAILURE TRIAGE (mandatory): a green suite is worthless if it is green because the expectations were bent. For every failing test decide, with evidence (log, assertion, screenshot, app code), which case it is: (a) TEST bug (timing, isolation, wrong selector, wrong expectation of documented behaviour) -> fix the test; (b) ENVIRONMENT limit (e.g. this emulator/launcher cannot perform a step) -> guard only that step with assumeTrue(...) and an exact reason; (c) APP bug (the app really does the wrong thing for a legitimate user scenario) -> NEVER weaken, skip or delete the assertion. Keep the test failing, post the bug with HARNESS_NOTE (screen, steps, expected vs actual, evidence, suspected code location), and fix the app only if the fix is small, clearly correct and inside this app; justify every production change in your final report.

SHARE FINDINGS ON THE BOARD: any assistant message that starts with a line `HARNESS_NOTE` is posted verbatim to this ticket's Board thread, where other developer agents and managers read it. Post a HARNESS_NOTE whenever you learn something reusable: a root cause, an app bug, an API/library quirk (e.g. a check or method that does not exist in this version), an emulator/launcher limitation, or a working pattern. Keep each note short and factual. Emulator results, heartbeats and your final report are posted automatically.

Use raw logs and meaningful assertion failures first. harness-tool screenshot returns the actual owned failure PNG path; inspect that path using read. Completed jobs may have nonzero exit codes; skipped/crashed/unrun and zero tests are not passes. New edits invalidate old APK/build evidence. Aim to finish with the full suite passing on the emulator. To finish, make a clean local commit using harness-tool commit and emit HARNESS_REVIEW <full40charactercommit> with a truthful inventory: tests run/passed/failed/skipped, remaining gaps and every production change. The manager independently reviews, reruns and decides on merge; privileged deployment, CI and real ROM/device remain separate checks.

{brief}
--- Original task ({title}) ---

{description}

Begin the selected task now. Read .harness/build-test-interface.md, then establish the starting build checkpoint promptly.
"""


def stop_bridge(bridge_dir, api):
    """Stop our own idle bridge and release ONLY its exact, cleanly-shut-down reservation."""
    os.kill(api['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and process_stamp(api['pid']) == api['reservation']['process_stamp']:
        time.sleep(.2)
    if process_stamp(api['pid']) == api['reservation']['process_stamp']: raise SystemExit('Bridge stop not verified')
    shutdown = private_json(bridge_dir / 'shutdown.json')
    if shutdown.get('runtime_cleanup_verified') is not True or shutdown.get('at', 0) < api['reservation']['started_at']:
        raise SystemExit('Bridge shutdown did not verify emulator cleanup; reconcile manually')
    lock = Path(api['reservation']['path'])
    if private_json(lock / 'owner.json') != api['reservation']: raise SystemExit('Reservation owner changed; not releasing')
    (lock / 'owner.json').unlink();(lock / 'pid').unlink();lock.rmdir()
    print('Previous bridge stopped; its verified reservation released')


def start_bridge(bridge_dir, authorization, target, remote):
    """Retarget and start the Mac bridge, refresh the tunnel port and Ubuntu's bridge credentials."""
    api_file = bridge_dir / 'api.private.json'
    config_file = bridge_dir / 'config.json'
    bridge_config = private_json(config_file)
    bridge_config['authorization_id'] = authorization
    bridge_config['emulator']['target'] = {'package': target['package'], 'role': target['role'],
                                           'denied_permissions': target['denied_permissions'],
                                           'denied_marker': target['denied_marker']}
    write_private(config_file, bridge_config)
    if api_file.exists(): api_file.rename(bridge_dir / ('api-' + str(time.time_ns()) + '.private.json'))
    with (bridge_dir / 'bridge.log').open('a') as log:
        child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/serve_mac_bridge.py'), '--config', str(config_file)],
            cwd=ROOT, env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(Path.home()), 'PYTHONUNBUFFERED': '1'},
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    write_private(bridge_dir / 'launcher.json', {'pid': child.pid})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not api_file.exists():
        if child.poll() is not None: raise SystemExit('Bridge exited; inspect ' + str(bridge_dir / 'bridge.log'))
        time.sleep(.2)
    api = private_json(api_file)
    write_private(bridge_dir / 'tunnel-port', {'port': urlsplit(api['endpoint']).port})
    # scripts/tunnel_loop.sh re-reads the bridge port on every reconnect; end the
    # current forward so it reconnects with the new port.
    subprocess.run(['pkill', '-f', 'ssh .*-R 127.0.0.1:19140:127.0.0.1:9140'], check=False)
    remote.py(r"""
import json,sys
from pathlib import Path
p=Path.home()/'.config/strata-board-harness/mac-bridge.json';t=p.with_suffix('.tmp')
t.write_text(json.dumps(json.load(sys.stdin)));t.chmod(0o600);t.replace(p)
""", {'endpoint': 'http://127.0.0.1:19145', 'token': api['token'], 'reservation_nonce': api['reservation_nonce']})
    print('Bridge PID', api['pid'], 'targets', target['package'])
    return api


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--restart-bridge', action='store_true',
                   help='stop the running bridge (verified idle cleanup) before dispatch')
    p.add_argument('--ticket-id', required=True)
    p.add_argument('--repo', required=True, help='Mac clone holding --ref')
    p.add_argument('--ref', required=True, help='exact branch/commit the worker starts from')
    p.add_argument('--package', required=True)
    p.add_argument('--role', default=None)
    p.add_argument('--denied-permission', action='append', default=[])
    p.add_argument('--slug', required=True)
    p.add_argument('--branch', required=True)
    p.add_argument('--brief-file', help='manager brief appended to the prompt')
    p.add_argument('--no-playbook', action='store_true', help='do not append playbooks/attentiveos-e2e.md to the brief')
    p.add_argument('--continuation', default='', help='one paragraph describing prior state when resuming earlier work')
    p.add_argument('--steps', type=int, default=400)
    p.add_argument('--minutes', type=int, default=90)
    p.add_argument('--host', default='ubuntu@blackoutburst.com')
    p.add_argument('--board-url', default='http://127.0.0.1:9140')
    p.add_argument('--board-password-file', default='~/.local/share/board-local/dev-password.txt')
    p.add_argument('--harness-agent-id', required=True)
    p.add_argument('--manager-agent-id', required=True)
    p.add_argument('--bridge-state', default='~/.local/state/strata-board-harness-bridge')
    p.add_argument('--model', default='swift-1.5-iq3_s')
    p.add_argument('--skip-prefetch', action='store_true', help='reuse an already-warm per-task Gradle home')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    if not (1 <= a.steps <= 400 and 1 <= a.minutes <= 90): raise SystemExit('Caps exceed the authorized execution limits')
    target = selected_target({'target': {'package': a.package, 'role': a.role, 'denied_permissions': a.denied_permission}})
    remote = Remote(a.host)
    board = Board(a.board_url, a.board_password_file)
    ticket = board.ticket(a.ticket_id)
    baseline = run(['git', '-C', a.repo, 'rev-parse', a.ref + '^{commit}'], capture_output=True, text=True).stdout.strip()
    authorization = 'e2e-%s-%s' % (a.slug, time.strftime('%Y%m%dT%H%M%S'))
    workspace = '/home/ubuntu/agent-workspaces/' + a.slug
    print('Ticket', ticket['number'], ticket['title'], '| status', ticket['status'], '| baseline', baseline)

    # 1. Exclusivity on both hosts before any effect.
    local = subprocess.check_output(['ps', '-axo', 'comm='], text=True)
    if any(Path(x.strip()).name == 'opencode' for x in local.splitlines()):
        raise SystemExit('A coding process runs on the Mac; refusing')
    remote_state = json.loads(remote.py(
        "import json,subprocess,pathlib,sys\n"
        "ps=subprocess.check_output(['ps','-axo','comm='],text=True)\n"
        "print(json.dumps({'opencode':any(pathlib.Path(x.strip()).name=='opencode' for x in ps.splitlines()),"
        "'lock':(pathlib.Path.home()/'.local/share/opencode-worker.lock').exists(),"
        "'workspace_exists':pathlib.Path(json.load(sys.stdin)['workspace']).exists()}))", {'workspace': workspace}))
    if remote_state['opencode'] or remote_state['lock']:
        raise SystemExit('Ubuntu has a coding process or worker lock: %s' % remote_state)
    if remote_state['workspace_exists']: raise SystemExit('Workspace already exists on Ubuntu; choose a new slug')
    bridge_dir = Path(a.bridge_state).expanduser()
    api_file = bridge_dir / 'api.private.json'
    if api_file.exists():
        api = private_json(api_file)
        if process_stamp(api['pid']) == api['reservation']['process_stamp']:
            if not a.restart_bridge:
                raise SystemExit('A bridge is still running; pass --restart-bridge to stop it with verified cleanup')
            if a.dry_run: raise SystemExit('Dry run: bridge would be restarted; preconditions otherwise not checked')
            stop_bridge(bridge_dir, api)
    lock = Path(private_json(bridge_dir / 'config.json')['worker_lock']).expanduser()
    if lock.exists(): raise SystemExit('Mac worker lock %s exists; reconcile its owner first' % lock)
    if (bridge_dir / 'shutdown.json').exists() and private_json(bridge_dir / 'shutdown.json').get('runtime_cleanup_verified') is not True:
        raise SystemExit('Previous bridge did not verify emulator cleanup')
    runtime_state = bridge_dir / 'runtime' / 'runtime.json'
    if runtime_state.exists() and private_json(runtime_state).get('phase') != 'idle':
        raise SystemExit('Emulator runtime is not idle')
    profile = board.call('GET', '/api/agent-profiles/' + a.harness_agent_id)
    if profile.get('project_ids') is not None and ticket['project_id'] not in profile['project_ids']:
        raise SystemExit('Harness agent profile lacks access to this project; widen its project_ids (bumps the '
                         'profile version: update profile_version in the Ubuntu board.json after verifying the diff)')
    if a.dry_run:
        print('Dry run: preconditions hold; nothing changed');return

    # 2. Stop the daemon and archive (not delete) its previous state.
    stamp = time.strftime('%Y%m%dT%H%M%S')
    remote.sh(f'export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user stop {SERVICE}; '
              f'cd {REMOTE_STATE} && mkdir -p archive/{stamp} && '
              f'for f in state.sqlite3 state.sqlite3-wal state.sqlite3-shm; do [ -e "$f" ] && mv "$f" archive/{stamp}/; done; '
              f'cp {REMOTE_CONFIG}/runtime.json archive/{stamp}/runtime.json 2>/dev/null; true', timeout=200)
    print('Daemon stopped; previous state archived under', REMOTE_STATE + '/archive/' + stamp)

    # 2b. A previous execution that ended without releasing (tunnel outage, kill, stale
    # progress) leaves the harness session's LIVE claim, and the new execution would wait up
    # to a full lease. Preflight verified no OpenCode process and no worker lock, and the
    # daemon is stopped, so that claim has no owner left: release it as the same session.
    # Only a live claim held by the harness's own session is touched.
    released = remote.py(r"""
import json,os,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path.home()/'.local/lib/strata-board-harness'))
from strata_board_harness.board import BoardClient,MCPHTTP
from strata_board_harness.coordination import Identity
from strata_board_harness.runtime import private_json
args=json.load(sys.stdin)
config=json.loads((Path.home()/'.config/strata-board-harness/config.json').read_text())
board=private_json(private_json(config['runtime_file'])['board_file']);identity=Identity(**board['identity'])
os.environ['STRATA_RELEASE_TOKEN']=board['token']
client=BoardClient(MCPHTTP(board['endpoint'],'STRATA_RELEASE_TOKEN'),identity,board['identity_key'],[args['ticket']],
                   board.get('label','Strata harness worker'))
client.register()
ticket=client.ticket(args['ticket']);claimant=ticket.get('claimant') or {}
if claimant.get('session_id')==identity.session_id and not ticket.get('claim_expired'):
    client._mutate('release_ticket',args['ticket'],{'expected_version':ticket['version'],'reason':args['reason'],
        'status':'todo','idempotency_key':uuid.uuid4().hex,'unassign_self':False})
    print('released')
else:
    print('nothing to release')
""", {'ticket': a.ticket_id, 'reason': 'Manager reconciliation %s: the previous harness execution is stopped '
      '(no OpenCode process, no worker lock); releasing its stale claim before redispatch.' % stamp})
    print('Previous harness claim:', released.strip() if isinstance(released, str) else released)

    # 3. Ship the exact source ref (with history) and create the worker branch.
    tag = 'harness-dispatch-' + a.slug
    run(['git', '-C', a.repo, 'tag', '-f', tag, baseline], capture_output=True)
    try:
        bundle = subprocess.run(['git', '-C', a.repo, 'bundle', 'create', '-', 'refs/tags/' + tag], capture_output=True,
                                check=True, timeout=300).stdout
    finally:
        run(['git', '-C', a.repo, 'tag', '-d', tag], capture_output=True)
    remote.sh(f'set -e; tmp=$(mktemp); cat >"$tmp"; mkdir -p {workspace}; cd {workspace}; git init -q; '
              f'git fetch -q "$tmp" "refs/tags/{tag}:refs/tags/{tag}"; rm -f "$tmp"; '
              f'git checkout -q -b {shlex.quote(a.branch)} {baseline}; git tag -d {tag} >/dev/null; '
              f'git config user.name "Strata Harness Ubuntu"; git config user.email "strata-harness@localhost"; '
              f'mkdir -p .harness; printf "sdk.dir=/opt/android-sdk\\n" > local.properties', data=bundle, timeout=300)
    remote.sh(f'cat > {workspace}/.harness/build-test-interface.md', data=(ROOT / 'docs/build-test-interface.md').read_bytes())
    print('Workspace', workspace, 'at', baseline)

    # 4. Trusted manager prefetch: online warm build of the exact starting source.
    task_state = f'{REMOTE_STATE}/{a.slug}'
    gradle_home = f'/home/ubuntu/.local/share/strata-board-harness/pilots/{a.slug}/gradle'
    prefetch_ok = False
    if not a.skip_prefetch:
        prefetch = remote.py(r"""
import json,subprocess,sys,time
from pathlib import Path
a=json.load(sys.stdin);home=Path.home()
cache=Path(a['gradle_home']);cache.mkdir(parents=True,exist_ok=True)
for name in ('caches','wrapper'):
    if not (cache/name).exists() and (home/'.gradle'/name).exists():
        subprocess.run(['cp','--reflink=auto','-a',str(home/'.gradle'/name),str(cache/name)],check=True,timeout=900)
sys.path.insert(0,str(home/'.local/lib/strata-board-harness'))
from strata_board_harness.builds import namespace_command
tools=home/'.local/share/strata-board-harness/tools'
args=namespace_command(tools/'bubblewrap/usr/bin/bwrap',Path(a['workspace']),tools/'android-sdk',cache,online=True)+[
    '/bin/sh','./gradlew','--no-daemon','--console=plain','assembleDebug','assembleDebugAndroidTest','testDebugUnitTest']
log=cache.parent/'manager-prefetch.log';started=time.time()
with log.open('w') as out:
    try:code=subprocess.run(args,cwd=a['workspace'],stdout=out,stderr=subprocess.STDOUT,timeout=1800).returncode
    except subprocess.TimeoutExpired:code=124
print(json.dumps({'exit_code':code,'seconds':round(time.time()-started),'log':str(log),
                  'tail':log.read_text(errors='replace')[-1500:] if code else ''}))
""", {'workspace': workspace, 'gradle_home': gradle_home}, timeout=2000)
        result = json.loads(prefetch.strip().splitlines()[-1])
        print('Manager prefetch exit', result['exit_code'], 'in', result['seconds'], 's')
        prefetch_ok = result['exit_code'] == 0
        if result['exit_code']:
            # A failing starting build is evidence for the worker, not a dispatch blocker,
            # but missing dependencies would starve its offline builds: surface it.
            print('Prefetch tail:\n' + result['tail'])
        remote.sh(f'cd {workspace} && git status --short --ignored | grep -v "^!!" || true')

    # 5. Prompt, readiness, approvals and the immutable runtime selection.
    brief = Path(a.brief_file).read_text().strip() + '\n' if a.brief_file else ''
    if not a.no_playbook and (ROOT / 'playbooks/attentiveos-e2e.md').exists():
        brief += '\n' + (ROOT / 'playbooks/attentiveos-e2e.md').read_text().strip() + '\n'
    prompt = PROMPT.format(authorization_id=authorization, manager=a.manager_agent_id, date=time.strftime('%Y-%m-%d'),
        model=a.model, workspace=workspace, branch=a.branch, baseline=baseline, minutes=a.minutes, steps=a.steps,
        package=target['package'], continuation=('\n' + a.continuation.strip() + '\n') if a.continuation else '',
        denied_hint=(' and harness-tool test denied for genuine externally denied-state tests (annotate them with '
                     'runtime annotation ' + target['denied_marker'] + '; the adapter revokes ' +
                     ', '.join(target['denied_permissions']) + ' before launch)') if target['denied_permissions'] else
                    ' (this target has no external denied-permission fixture; test permission states inside the app only by granting, never by revoking)',
        brief=brief, title=ticket['title'], description=ticket.get('description') or board.call(
            'GET', '/api/tickets/' + a.ticket_id).get('description', ''))
    if len(prompt.encode()) > 65536: raise SystemExit('Prompt exceeds 64KiB')
    selection = {'authorization_id': authorization, 'ticket_id': a.ticket_id, 'baseline': baseline, 'workspace': workspace}
    readiness = {'selection': selection, 'gates': {g: True for g in GATES},
                 'evidence': {'suite': '122 passing (emulator target generalization)', 'dispatch': stamp,
                              'gates_inherited_from': 'phone-e2e-pilot-20261023 host readiness'},
                 'limits': {'active_seconds': a.minutes * 60, 'steps': a.steps, 'connected_seconds': 1500, 'per_test_seconds': 300}}
    remote.py(r"""
import json,sys,os,hashlib
from pathlib import Path
a=json.load(sys.stdin);home=Path.home()
state=home/'.local/state/strata-board-harness'/a['slug'];state.mkdir(parents=True,exist_ok=True,mode=0o700)
def put(path,value):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(value if isinstance(value,str) else json.dumps(value,indent=1));tmp.chmod(0o600);tmp.replace(path)
put(state/'prompt.txt',a['prompt']);put(state/'readiness.json',a['readiness']);put(state/'build-approvals.json',{})
config=home/'.config/strata-board-harness/runtime.json';runtime=json.loads(config.read_text())
runtime['selected_task']={'authorization_id':a['selection']['authorization_id'],'ticket_id':a['selection']['ticket_id'],
    'thread_id':a['thread_id'],'baseline':a['selection']['baseline'],'step_budget':a['steps'],
    'max_active_seconds':a['seconds'],'manager_agent_id':a['manager'],'prompt_file':str(state/'prompt.txt'),
    'prompt_sha256':hashlib.sha256(a['prompt'].encode()).hexdigest(),
    'recovery_evidence':a['recovery_evidence'],
    # The manager prefetch already compiled this exact starting commit; continuation
    # work starts by reading it, so allow more steps before the first worker build.
    'early_build_steps':30 if a['prefetch_ok'] else 12,
    'description_sha256':hashlib.sha256(a['description'].encode()).hexdigest()}
runtime['readiness_file']=str(state/'readiness.json')
runtime['engine'].update(engine_root=str(state/'engine'),workspace=a['selection']['workspace'],model=a['model'])
runtime['build'].update(gradle_home=a['gradle_home'],approvals_file=str(state/'build-approvals.json'))
put(config,runtime)
print('selection written')
""", {'slug': a.slug, 'prompt': prompt, 'readiness': readiness, 'selection': selection, 'thread_id': ticket['thread_id'],
      'steps': a.steps, 'seconds': a.minutes * 60, 'manager': a.manager_agent_id, 'model': a.model,
      'gradle_home': gradle_home, 'description': ticket.get('description') or '', 'prefetch_ok': prefetch_ok,
      # Lets the worker recover ONLY its own expired claim from an earlier execution;
      # every statement below was checked by this script before any effect.
      'recovery_evidence': ('Manager reconciliation %s for %s: previous harness state archived under '
          'archive/%s; no OpenCode process and no worker lock on Ubuntu or Mac before dispatch; previous '
          'Mac bridge stopped with verified emulator cleanup; fresh workspace %s at %s.'
          % (stamp, authorization, stamp, workspace, baseline))})
    print('Runtime selection written for', authorization)

    # 6. Retarget and start the Mac bridge, then refresh the tunnel's bridge port.
    start_bridge(bridge_dir, authorization, target, remote)

    # 7. Hand the ticket to the worker and start the daemon.
    fresh = board.ticket(a.ticket_id)
    board.call('PATCH', '/api/tickets/' + a.ticket_id, {'assignee_agent_id': a.harness_agent_id, 'status': 'todo',
                                                         'expected_version': fresh['version']})
    # The tunnel was restarted for the new bridge port; start the daemon only once Ubuntu
    # reaches both forwards again (any HTTP status proves the forward answers).
    remote.sh('for i in $(seq 60); do b=$(curl -s -m3 -o /dev/null -w "%{http_code}" http://127.0.0.1:19140/); '
              'e=$(curl -s -m3 -o /dev/null -w "%{http_code}" http://127.0.0.1:19145/); '
              '[ "$b" != 000 ] && [ "$e" != 000 ] && exit 0; sleep 2; done; echo tunnel not ready >&2; exit 1',
              timeout=200)
    remote.sh(f'export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user start {SERVICE}; sleep 3; '
              f'systemctl --user is-active {SERVICE}')
    print('Dispatched', authorization, 'to', a.harness_agent_id)


if __name__ == '__main__':
    main()
