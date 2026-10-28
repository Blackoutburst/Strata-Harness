#!/usr/bin/env python3
"""Run the loopback-only bridge and reserve coding-worker exclusivity on Mac.

Shutdown preserves the reservation until the manager verifies remote execution
and emulator effects. A lease age or a server exit never clears another lock.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.bridge import MacBridge
from strata_board_harness.emulator import EmulatorRuntime
from strata_board_harness.managed_engine import write_private,process_stamp
from strata_board_harness.runtime import private_json


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=private_json(args.config);directory=Path(config['state_dir']).expanduser().resolve()
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=Path(config['worker_lock']).expanduser()
    # Exclusivity is the worker lock: the harness's coding engine runs on Ubuntu, and an
    # OpenCode session the owner runs on this Mac is not a worker.
    lock.mkdir(mode=0o700)
    reservation={'path':str(lock),'nonce':secrets.token_hex(16),'pid':os.getpid(),
        'process_stamp':process_stamp(os.getpid()),'purpose':'human_authorized_phone_harness_pilot',
        'authorization_id':config['authorization_id'],'started_at':time.time()}
    write_private(lock/'owner.json',reservation);(lock/'pid').write_text(str(os.getpid()))
    runtime=EmulatorRuntime(config['emulator'],directory/'runtime')
    runtime.verify()
    bridge=MacBridge(runtime,directory/'bridge',reservation)
    endpoint=bridge.start()
    write_private(directory/'api.private.json',{'endpoint':endpoint,'token':bridge.token,
        'reservation_nonce':reservation['nonce'],'reservation':reservation,'pid':os.getpid()})
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    print('Selected emulator bridge ready on owned loopback; cross-host reservation held',flush=True)
    try:stop.wait()
    finally:
        clean=bridge.close()
        write_private(directory/'shutdown.json',{'runtime_cleanup_verified':clean,'at':time.time(),
            'reservation_retained_for_remote_process_reconciliation':True})


if __name__=='__main__':main()
