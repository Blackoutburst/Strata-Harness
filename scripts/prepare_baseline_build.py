"""Manager-only baseline build readiness on Ubuntu; not a developer pilot."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def main():
    root=Path(__file__).resolve().parents[1]
    # Operator supplies the known source repository. No implementation branches
    # or outputs are transferred; git archive selects the exact baseline tree.
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--source-repo',required=True);parser.add_argument('--host',required=True)
    parser.add_argument('--ssh-control',required=True);args=parser.parse_args()
    baseline='19d7458debbe540c9cb7f0061805f7af766cdf5a'
    ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-S',args.ssh_control,args.host]
    archive=subprocess.check_output(['git','-C',args.source_repo,'archive','--format=tar.gz',baseline])
    print('Compressed baseline bytes',len(archive),flush=True)
    data=subprocess.run(ssh+['mkdir -p ~/.local/share/strata-board-harness-setup/baseline-build/workspace && tar -xzf - -C ~/.local/share/strata-board-harness-setup/baseline-build/workspace'],
        input=archive,capture_output=True,timeout=180)
    if data.returncode:raise RuntimeError('Baseline transfer failed')
    print('Baseline transfer complete',flush=True)
    script="""from pathlib import Path
import subprocess,sys,json,time
root=Path.home()/'.local/share/strata-board-harness-setup/baseline-build'
cache=root/'gradle';cache.mkdir(exist_ok=True)
for name in ['caches','wrapper']:
 if not (cache/name).exists():subprocess.run(['cp','--reflink=auto','-a',str(Path.home()/'.gradle'/name),str(cache/name)],check=True,timeout=180)
sys.path.insert(0,str(Path.home()/'.local/lib/strata-board-harness'))
from strata_board_harness.builds import namespace_command
workspace=root/'workspace';sdk=Path.home()/'Android/Sdk'
(workspace/'local.properties').write_text('sdk.dir=/opt/android-sdk\\n')
args=namespace_command(Path.home()/'.local/share/strata-board-harness/tools/bubblewrap/usr/bin/bwrap',workspace,sdk,cache,online=True)+['/bin/sh','./gradlew','--no-daemon','--console=plain','assembleDebug','assembleDebugAndroidTest','testDebugUnitTest']
started=time.time()
with (root/'warm-build.log').open('w') as output:
 try:r=subprocess.run(args,cwd=workspace,stdout=output,stderr=subprocess.STDOUT,timeout=900);code=r.returncode
 except subprocess.TimeoutExpired:code=124
(root/'warm-build.json').write_text(json.dumps({'exit_code':code,'seconds':time.time()-started,'source_baseline':'19d7458debbe540c9cb7f0061805f7af766cdf5a','trusted_baseline_prefetch_network':True,'pilot_commands_offline':True},indent=2))
print('Baseline warm build exit',code)
print((root/'warm-build.log').read_text()[-6000:])
"""
    result=subprocess.run(ssh+['python3 -c '+shlex.quote(script)],capture_output=True,text=True,timeout=1110)
    (root/'verification/2026-10-23-ubuntu-baseline-warm-build.txt').write_text(result.stdout+result.stderr)
    print(result.stdout,result.stderr[-2000:])
    raise SystemExit(result.returncode)


if __name__=='__main__':main()
