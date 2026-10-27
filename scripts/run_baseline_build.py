"""One manager-owned, fixed baseline build; preserve every attempt separately."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.builds import namespace_command


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--offline',action='store_true');parser.add_argument('--clean',action='store_true');args=parser.parse_args()
    root=Path.home()/'.local/share/strata-board-harness-setup/baseline-build'
    tools=Path.home()/'.local/share/strata-board-harness/tools'
    tag=('offline-' if args.offline else 'warm-')+str(time.time_ns())
    log=root/(tag+'.log');receipt=root/(tag+'.json')
    command=namespace_command(tools/'bubblewrap/usr/bin/bwrap',root/'workspace',tools/'android-sdk',root/'gradle',online=not args.offline)+[
        '/bin/sh','./gradlew',*(['--offline'] if args.offline else []),'--no-daemon','--console=plain',
        *(['clean'] if args.clean else []),'assembleDebug','assembleDebugAndroidTest','testDebugUnitTest']
    started=time.time()
    with log.open('w') as output:
        try:result=subprocess.run(command,stdout=output,stderr=subprocess.STDOUT,timeout=900);code=result.returncode
        except subprocess.TimeoutExpired:code=124
    result={'exit_code':code,'seconds':time.time()-started,'baseline':'19d7458debbe540c9cb7f0061805f7af766cdf5a',
        'offline':args.offline,'jdk':'private javac17.0.20.1','sdk':'private build-tools36.0.0','log':str(log)}
    receipt.write_text(json.dumps(result,indent=2))
    (root/'latest-build.json').write_text(json.dumps(dict(result,receipt=str(receipt)),indent=2))
    print(json.dumps(result));print(log.read_text()[-8000:])
    return code


if __name__=='__main__':raise SystemExit(main())
