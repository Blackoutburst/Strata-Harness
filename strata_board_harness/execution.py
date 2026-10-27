"""Assemble selected execution adapters after operator readiness admission."""

import hashlib
import json
import os
from pathlib import Path
import time

from .bridge import EmulatorClient
from .builds import AndroidBuilds
from .gateway import JobGateway
from .managed_engine import ManagedEngine
from .model_proxy import ModelProxy
from .runtime import private_json
from .worker import ExecutionWorker


GATES=('fake_lifecycle','ubuntu_engine_rtk','managed_engine_api','offline_android_build',
       'emulator_rollback','serial_runtime_bridge','strata_tool_calls','image_path_checked',
       'cross_host_reconciled','source_boundary')


class Validation:
    def __init__(self,builds,bridge,directory):
        self.builds,self.bridge=builds,bridge
        def checkpoint(argument,cancelled):
            # This handler runs inside the gateway's current job thread. Only
            # downstream process/effect cleanup belongs in its result; asking
            # the enclosing gateway whether every thread has exited is circular.
            return {'exit_code':0,'output':json.dumps(self.checkpoint()),'cleanup_verified':self.builds.clean()}
        def screenshot(argument,cancelled):
            return {'exit_code':0,'output':json.dumps(self.bridge.failure_image(self.builds.workspace)),
                    'cleanup_verified':self.builds.clean()}
        self.gateway=JobGateway(directory,{'build':builds.build,'test':builds.test,'checkpoint':checkpoint,
            'commit':builds.commit,'screenshot':screenshot})
        self.endpoint=self.gateway.start()
        os.environ['HARNESS_GATEWAY_URL']=self.endpoint
        os.environ['HARNESS_GATEWAY_TOKEN']=self.gateway.token

    def status(self):
        value=self.builds.status()
        value['source_sha256']=self.builds.checkpoint(include_runtime=False)['source_sha256']
        jobs=self.gateway.status()['jobs']
        completed=[job for job in jobs if job.get('state')=='completed']
        if completed:
            latest=max(completed,key=lambda job:job['finished_at'])
            value['last_feedback']={k:latest[k] for k in ('id','finished_at','exit_code')}
            value['last_feedback']['operation']=latest.get('operation')
            counts=((latest.get('instrumentation') or {}).get('counts')) or {}
            if counts:
                value['last_feedback']['summary']={
                    'counts':{k:counts.get(k) for k in ('passed','failed','errors','skipped','declared_total','unrun','crashed')},
                    'failed':[t.get('class','').split('.')[-1]+'.'+t.get('test','')
                              for t in (latest.get('instrumentation') or {}).get('tests',[]) if t.get('code') not in (0,)][:12]}
        build_jobs=[job for job in completed if job.get('operation')=='build']
        if build_jobs and max(build_jobs,key=lambda job:job['finished_at']).get('requires_manager_review'):
            # The old receipt remains a failed build, but a reviewed exact hash
            # or a reverted control file can resolve its outstanding request.
            changes=self.builds.control_changes()
            if changes:value['requires_manager_review']=changes
        return value

    def activity(self):return self.gateway.activity()
    def checkpoint(self):return self.builds.checkpoint()
    def clean(self):return self.gateway.clean() and self.builds.clean()

    def pause(self):
        self.gateway.cancel.set();self.builds.cancel()
        deadline=time.monotonic()+75
        while time.monotonic()<deadline:
            if self.clean():return
            time.sleep(.1)
        raise RuntimeError('Validation pause cleanup requires reconciliation')

    def resume(self):
        if not self.clean():raise RuntimeError('Cannot resume unreconciled validation')
        # The bridge's cancellation fence is only reset at a verified clean
        # checkpoint within the same still-authorized pilot.
        self.bridge.resume()
        if getattr(self.builds, 'approvals_file', None):
            self.builds.approvals=private_json(self.builds.approvals_file)
        self.gateway.cancel.clear()

    def cancel(self):self.pause()
    def close(self):
        clean=self.gateway.stop(timeout=75)
        if hasattr(self,'model_proxy'):self.model_proxy.close()
        return clean


def from_config(config,journal,client):
    selection=dict(config['selected_task'])
    readiness=private_json(config['readiness_file'])
    required={'authorization_id':selection['authorization_id'],'ticket_id':selection['ticket_id'],
              'baseline':selection['baseline'],'workspace':str(Path(config['engine']['workspace']).resolve())}
    if readiness.get('selection')!=required or any(readiness.get('gates',{}).get(gate) is not True for gate in GATES):
        raise ValueError('Selected execution readiness is incomplete or belongs to another scope')
    step_budget=selection['step_budget'];maximum=selection.get('max_active_seconds',5400)
    if type(step_budget) is not int or not 1<=step_budget<=400 or type(maximum) is not int or not 1<=maximum<=5400:
        raise ValueError('Selected pilot exceeds authorized execution caps')
    prompt=Path(selection['prompt_file']).read_text()
    if len(prompt.encode())>65536 or hashlib.sha256(prompt.encode()).hexdigest()!=selection['prompt_sha256']:
        raise ValueError('Selected prompt changed')
    selection['prompt']=prompt
    settings=dict(config['engine']);settings.update(ticket_id=selection['ticket_id'],step_budget=step_budget)
    workspace=Path(settings['workspace']).resolve()
    git_dir=workspace/'.git'
    if not git_dir.is_dir() or git_dir.is_symlink():raise ValueError('Pilot requires its own Git object boundary')
    excluded=git_dir/'info/exclude'
    previous=excluded.read_text() if excluded.exists() else ''
    if '\n.harness/\n' not in '\n'+previous:excluded.write_text(previous+'\n.harness/\n')
    bridge=EmulatorClient(private_json(config['bridge_file']),Path(settings['engine_root'])/'bridge')
    if bridge.guard() is not True:raise ValueError('Current cross-host reservation not verified')
    builds=AndroidBuilds(settings['workspace'],selection['baseline'],config['build']['bwrap'],config['build']['sdk'],
        config['build']['gradle_home'],Path(settings['engine_root'])/'commands',runtime_adapter=bridge,
        approvals=private_json(config['build']['approvals_file']) if config['build'].get('approvals_file') else {})
    builds.approvals_file=config['build'].get('approvals_file')
    validation=Validation(builds,bridge,Path(settings['engine_root'])/'jobs')
    identity=journal.identity
    def authorize(envelope,body,task):
        author=envelope.get('author',{})
        return (task==selection['ticket_id'] and envelope.get('thread_id')==selection['thread_id']
            and author.get('owner_id')==identity.owner_id and
            (author.get('kind')=='human' or (author.get('kind')=='agent' and author.get('agent_id')==selection['manager_agent_id'])))
    proxy=ModelProxy(config['backend_url'],os.environ['STRATA_API_KEY'],settings['model'],Path(settings['engine_root'])/'model-proxy')
    validation.model_proxy=proxy
    try:
        endpoint=proxy.start()
    except Exception:
        validation.close();raise
    os.environ['STRATA_MODEL_PROXY_TOKEN']=proxy.token
    settings['opencode_config']={
        'model':settings['provider']+'/'+settings['model'],'small_model':settings['provider']+'/'+settings['model'],
        'provider':{settings['provider']:{'npm':'@ai-sdk/openai-compatible','name':'Strata',
            'options':{'baseURL':endpoint,'apiKey':'{env:STRATA_MODEL_PROXY_TOKEN}'},
            'models':{settings['model']:{'name':'Selected Swift IQ3_S','attachment':True,'tool_call':True,
                # Strata serves 262144 tokens; declaring less leaves room for OpenCode to compact
                # before one large tool result overflows the window (it never truncates requests).
                'modalities':{'input':['text','image'],'output':['text']},'limit':{'context':200000,'output':8192},
                'options':{'chat_template_kwargs':{'enable_thinking':False}}}}}},
        'agent':{'strata-harness':{'mode':'primary','steps':step_budget}},
        'permission':{'*':'deny','read':{'*':'allow','*.env*':'deny','.git/*':'deny'},
            'edit':{'*':'allow','.git/*':'deny','**/.git/*':'deny'},'glob':'allow','grep':'allow',
            'list':'allow','bash':{'*':'deny','ls':'allow','rtk ls':'allow','git status --short':'allow',
                'rtk git status --short':'allow','harness-tool *':'allow'},'external_directory':'deny'}}
    try:
        engine=ManagedEngine(journal_store(journal),journal,settings,bridge.guard,validation.clean,authorize)
        worker=ExecutionWorker(journal_store(journal),journal,client,engine,validation,selection,time.time)
        return worker
    except Exception:
        validation.close();raise


def journal_store(journal):
    # Runtime attaches the actual Store explicitly, never opens a second SQLite
    # writer or guesses a state path from source/Board text.
    if not hasattr(journal,'store'):raise ValueError('Runtime journal lacks its owning Store')
    return journal.store
