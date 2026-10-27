"""Reconcile an idle owned bridge before an explicit readiness-time upgrade.

Refuses any remote coding process/lock or unreconciled emulator effect. Never use
this to change a live coding worker or clear an unrelated reservation.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.managed_engine import process_stamp,write_private
from strata_board_harness.runtime import private_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--state-dir',required=True);p.add_argument('--host',required=True)
    p.add_argument('--ssh-control',required=True);p.add_argument('--remote-port',type=int,default=19145);a=p.parse_args()
    root=Path(a.state_dir).expanduser().resolve();old=private_json(root/'api.private.json')
    ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-S',a.ssh_control,a.host]
    code="from pathlib import Path;import subprocess;s=subprocess.check_output(['ps','-axo','comm='],text=True);assert not any(Path(x.strip()).name=='opencode' for x in s.splitlines());assert not (Path.home()/'.local/share/opencode-worker.lock').exists()"
    subprocess.run(ssh+['python3 -c '+shlex.quote(code)],check=True,capture_output=True,timeout=15)
    if process_stamp(old['pid'])!=old['reservation']['process_stamp']:raise RuntimeError('Bridge process changed')
    os.kill(old['pid'],signal.SIGTERM)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline and process_stamp(old['pid']):time.sleep(.1)
    if process_stamp(old['pid']):raise RuntimeError('Bridge stop not verified')
    clean=private_json(root/'shutdown.json')
    if clean.get('runtime_cleanup_verified') is not True:raise RuntimeError('Emulator effects not clean')
    lock=Path(old['reservation']['path'])
    if private_json(lock/'owner.json')!=old['reservation']:raise RuntimeError('Reservation owner changed')
    (lock/'owner.json').unlink();(lock/'pid').unlink();lock.rmdir()
    (root/'api.private.json').rename(root/('api-'+str(time.time_ns())+'.private.json'))
    old_port=urlsplit(old['endpoint']).port
    subprocess.run(['ssh','-S',a.ssh_control,'-O','cancel','-R',f'127.0.0.1:{a.remote_port}:127.0.0.1:{old_port}',a.host],check=True,capture_output=True,timeout=10)
    with (root/'bridge.log').open('a') as log:
        child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('serve_mac_bridge.py')),'--config',str(root/'config.json')],
            cwd=Path(__file__).resolve().parents[1],env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':str(Path.home()),'PYTHONUNBUFFERED':'1'},
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    write_private(root/'launcher.json',{'pid':child.pid})
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and not (root/'api.private.json').exists():
        if child.poll() is not None:raise RuntimeError('Updated bridge exited; inspect private log')
        time.sleep(.1)
    new=private_json(root/'api.private.json');port=urlsplit(new['endpoint']).port
    subprocess.run(['ssh','-S',a.ssh_control,'-O','forward','-R',f'127.0.0.1:{a.remote_port}:127.0.0.1:{port}',a.host],check=True,capture_output=True,timeout=10)
    config={'endpoint':f'http://127.0.0.1:{a.remote_port}','token':new['token'],'reservation_nonce':new['reservation_nonce']}
    script="from pathlib import Path;import json,sys;p=Path.home()/'.config/strata-board-harness/mac-bridge.json';old=json.loads(p.read_text());assert old['reservation_nonce']=="+repr(old['reservation_nonce'])+";t=p.with_suffix('.tmp');t.write_text(json.dumps(json.load(sys.stdin)));t.chmod(0o600);t.replace(p)"
    subprocess.run(ssh+['python3 -c '+shlex.quote(script)],input=json.dumps(config),text=True,check=True,capture_output=True,timeout=15)
    print('Owned idle bridge updated after remote process/lock and emulator cleanup checks; new PID',new['pid'])


if __name__=='__main__':main()
