"""One trusted baseline APK bridge smoke, separate from all model results."""
import argparse
import json
from pathlib import Path
import threading
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.bridge import EmulatorClient
from strata_board_harness.runtime import private_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--scenario',default='normal',choices=['normal','denied']);a=p.parse_args()
    root=Path.home()/'.local/share/strata-board-harness-setup'
    client=EmulatorClient(private_json(Path.home()/'.config/strata-board-harness/mac-bridge.json'),root/'baseline-bridge')
    if client.guard() is not True:raise RuntimeError('Cross-host reservation not verified')
    started=time.time();result=client.test(root/'baseline-build/workspace',threading.Event(),scenario=a.scenario)
    result.update(experiment='baseline_adapter_smoke_only',seconds=time.time()-started)
    Path(a.output).write_text(json.dumps(result,indent=2))
    print(json.dumps({k:result.get(k) for k in ['experiment','exit_code','instrumentation','cleanup_verified','seconds']},indent=2))
    return 0 if result.get('exit_code')==0 and result.get('cleanup_verified') is True else 1


if __name__=='__main__':raise SystemExit(main())
