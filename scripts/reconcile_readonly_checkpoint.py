"""Explicit operator repair for the first pilot's circular checkpoint fence.

Never used by the model or daemon. Requires a stopped service/owned process,
unchanged baseline source, exact failed read-only job IDs and the same live claim.
It preserves the session, consumed steps/time and all original failure receipts.
"""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.board import BoardClient,MCPHTTP
from strata_board_harness.bridge import EmulatorClient
from strata_board_harness.coordination import Identity,Journal
from strata_board_harness.gateway import JobGateway
from strata_board_harness.managed_engine import ManagedEngine,group_members,process_stamp,write_private
from strata_board_harness.runtime import private_json
from strata_board_harness.supervisor import InstanceLock,Store


def main():
    p=argparse.ArgumentParser();p.add_argument('--ticket',required=True);p.add_argument('--session',required=True)
    p.add_argument('--previous-pid',type=int,required=True);p.add_argument('--job',action='append',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--resume-released',action='store_true')
    p.add_argument('--resume-service-policy',action='store_true');p.add_argument('--resume-pagination',action='store_true');a=p.parse_args()
    if a.resume_service_policy or a.resume_pagination:a.resume_released=True
    home=Path.home();directory=home/'.local/state/strata-board-harness';cfg=home/'.config/strata-board-harness'
    env=dict(os.environ,XDG_RUNTIME_DIR='/run/user/'+str(os.getuid()))
    assert subprocess.run(['systemctl','--user','is-active','--quiet','strata-board-harness.service'],env=env).returncode!=0
    with_context=InstanceLock(directory)
    store=Store(directory)
    try:
        config=private_json(cfg/'runtime.json');selection=config['selected_task'];settings=dict(config['engine'])
        assert selection['ticket_id']==a.ticket
        engine=store.get('managed_engine');worker=store.get('execution_worker')
        assert engine['session_id']==a.session and engine['process']['pid']==a.previous_pid
        assert not process_stamp(a.previous_pid) and not group_members(a.previous_pid)
        assert not any(Path(row.strip()).name=='opencode' for row in subprocess.check_output(['ps','-axo','comm='],text=True).splitlines())
        assert engine['steps']<selection['step_budget'] and worker['active_seconds']<selection['max_active_seconds']
        if a.resume_released:
            assert engine['phase']=='stopped' and engine['cleanup_verified'] is True
            assert engine['steps']==(14 if (a.resume_service_policy or a.resume_pagination) else 10)
            assert worker.get('manual_infrastructure_continuations',1)<(4 if a.resume_pagination else (3 if a.resume_service_policy else 2))
        work=Path(settings['workspace'])
        assert subprocess.check_output(['git','-C',str(work),'rev-parse','HEAD'],text=True).strip()==selection['baseline']
        assert not subprocess.check_output(['git','-C',str(work),'status','--porcelain'],text=True)
        jobs=Path(settings['engine_root'])/'jobs'
        assert {f.stem for f in jobs.glob('job-*.json')}==set(a.job)
        originals={}
        for identifier in a.job:
            job=private_json(jobs/(identifier+'.json'))
            if (a.resume_service_policy or a.resume_pagination) and job['operation']=='build':
                assert job['state']=='completed' and job['exit_code']==1 and job['cleanup_verified'] is True
                assert 'NETLINK_ROUTE socket: Address family not supported by protocol' in Path(job['log']).read_text()
            else:
                assert job['operation']=='checkpoint' and job['state']=='reconciliation_required' and job['error_type']=='RuntimeError'
                if a.resume_released:
                    assert job['cleanup_verified'] is True and job.get('manager_reconciliation',{}).get('owned_process_and_group_gone') is True
                else:assert job['cleanup_verified'] is False
            originals[identifier]=job
        if a.resume_service_policy:
            proof=private_json(directory/'phone-e2e-pilot-20261023/build-policy-proof.json')
            assert proof['exit_code']==0 and proof['offline'] is True and proof['baseline']==selection['baseline']
            assert proof['address_families']=='AF_UNIX AF_INET AF_INET6 AF_NETLINK'
            assert 'AF_NETLINK' in (home/'.config/systemd/user/strata-board-harness.service').read_text()
        if a.resume_pagination:
            proof_path=home/'.local/share/strata-board-harness-setup/managed-engine-pagination.json'
            proof=json.loads(proof_path.read_text())
            assert all(proof[k] is True for k in ['passed','actual_paginated_history_complete',
                'history_seed_without_model_requests','reattach_same_session','process_cleanup_verified','worker_lock_absent'])
        bridge=EmulatorClient(private_json(config['bridge_file']),Path(settings['engine_root'])/'bridge')
        assert bridge.guard() and bridge.clean()
        board=private_json(config['board_file']);identity=Identity(**board['identity'])
        os.environ['CHECKPOINT_REPAIR_OWN_TOKEN']=board['token']
        client=BoardClient(MCPHTTP(board['endpoint'],'CHECKPOINT_REPAIR_OWN_TOKEN'),identity,board['identity_key'],[a.ticket],board['label'],[selection['thread_id']])
        client.register();ticket=client.ticket(a.ticket)
        if a.resume_released:assert ticket['status']=='todo' and ticket.get('claimant') is None
        else:assert ticket['status']=='in_progress' and ticket['lease_expires_at']>time.time()+120
        claimant=ticket['claimant']
        assert ticket['assignee_agent_id']==identity.agent_id
        if not a.resume_released:
            assert (claimant.get('agent_id')==identity.agent_id and claimant.get('owner_id')==identity.owner_id
                    and claimant.get('device_id')==identity.device_id and claimant.get('session_id')==identity.session_id)
        row=store.connection.execute('SELECT snapshot,pending,fenced FROM board_tasks WHERE ticket_id=?',(a.ticket,)).fetchone()
        assert row and not row[1] and not row[2] and json.loads(row[0])['version']==ticket['version']
        receipt={'kind':'manager_explicit_readonly_checkpoint_reconciliation','at':datetime.now(timezone.utc).isoformat(),
            'ticket_id':a.ticket,'session_id':a.session,'previous_pid':a.previous_pid,'jobs':a.job,
            'owned_process_and_group_gone':True,'source_unchanged_at_baseline':True,'emulator_cleanup_verified':True,
            'original_gateway_failure':'Checkpoint handler asked whether its own gateway thread had exited; no build, install or app edit occurred.',
            'steps_preserved':engine['steps'],'automatic_retry':False,'applied':a.apply}
        if a.resume_released:
            receipt.update(kind='manager_explicit_same_pilot_startup_reconciliation',
                original_gateway_failure='Old appended listening URL was read before restarted API listened; no new model request or source edit occurred.',
                continuation_number=2,automatic_future_continuations=False)
        if a.resume_service_policy:
            receipt.update(kind='manager_explicit_same_pilot_service_policy_reconciliation',continuation_number=3,
                original_gateway_failure='Service socket-family policy omitted AF_NETLINK required for bwrap loopback setup. Exact clean offline baseline build passed in an owned transient unit with the corrected policy.',
                service_policy_proof=proof)
        if a.resume_pagination:
            receipt.update(kind='manager_explicit_same_pilot_pagination_reconciliation',continuation_number=4,
                original_gateway_failure='v1 history requires X-Next-Cursor opaque header; message-ID pagination returned HTTP error. Actual installed-engine 25-message no-generation pagination and restart probe passed.',
                pagination_proof=proof)
        if not a.apply:print(json.dumps(receipt,indent=2));return
        evidence=directory/'phone-e2e-pilot-20261023'/('pagination-repair' if a.resume_pagination else ('service-policy-repair' if a.resume_service_policy else ('startup-repair' if a.resume_released else 'checkpoint-repair')))
        evidence.mkdir(mode=0o700,parents=True,exist_ok=False)
        backup=sqlite3.connect(evidence/'state-before.sqlite3');store.connection.backup(backup);backup.close();(evidence/'state-before.sqlite3').chmod(0o600)
        for identifier,job in originals.items():
            write_private(evidence/(identifier+'.original.json'),job)
            if not a.resume_released:
                log=jobs/(identifier+'.log')
                log.write_text('Original job had no exception log. Manager diagnosis and effect checks:\n'+json.dumps(receipt,indent=2));log.chmod(0o600)
                write_private(jobs/(identifier+'.json'),{**job,'cleanup_verified':True,'log':str(log),
                    'manager_reconciliation':receipt,'passed':False})
        journal=Journal(store,identity)
        settings.update(ticket_id=a.ticket,step_budget=selection['step_budget'])
        gateway=JobGateway(jobs,{'checkpoint':lambda *_:None})
        managed=ManagedEngine(store,journal,settings,bridge.guard,lambda:gateway.clean() and bridge.clean(),lambda *_:False)
        assert managed.stop('manager_reconciled_readonly_checkpoint_failure')
        bridge.resume()
        stopped=store.get('daemon',{}).get('heartbeat_at')
        end=datetime.fromisoformat(stopped).timestamp() if stopped else time.time()
        # Account conservatively for all time since original admission through
        # the stopped daemon, including infrastructure waiting; never reset it.
        worker['active_seconds']=max(worker['active_seconds'],end-engine['active_started_at'])
        assert worker['active_seconds']<selection['max_active_seconds']
        worker.update(phase='developing',stop_reason=None,previous_tick=None,last_backend=None,
            infrastructure_reconciliation=receipt,manual_infrastructure_continuations=4 if a.resume_pagination else (3 if a.resume_service_policy else (2 if a.resume_released else 1)))
        if a.resume_released:worker['manager_resume_claim']={'session_id':a.session,'receipt':str(evidence/'receipt.json')}
        if a.resume_service_policy:
            worker['first_build_started_at']=min(job['started_at'] for job in originals.values() if job['operation']=='build')
        managed.state.update(phase='new',cleanup_verified=False,stop_reason=None,progress_at=time.time(),
            manager_relaunch_authorization=receipt)
        for key in ('process','lock','endpoint'):managed.state.pop(key,None)
        managed.save()
        control=store.get('coordination_control');control.update(mode='idle',stop_reason=None,quiesce_request=None)
        control.pop('quiesce_error',None);control.pop('release_completed',None)
        store.save({'execution_worker':worker,'coordination_control':control,
                    'shutdown':{'state':'reconciled_readonly_checkpoint_failure','receipt':receipt}},
                   ('manager_reconciliation',receipt))
        write_private(evidence/'receipt.json',{**receipt,'active_seconds_preserved':worker['active_seconds']})
        print(json.dumps({**receipt,'active_seconds_preserved':worker['active_seconds']},indent=2))
    finally:
        os.environ.pop('CHECKPOINT_REPAIR_OWN_TOKEN',None);store.close();with_context.close()


if __name__=='__main__':main()
