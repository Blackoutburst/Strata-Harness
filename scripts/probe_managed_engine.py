"""Actual owned OpenCode + EngineHTTP + gateway preflight using a fake model.

Uses the already-owned cross-host bridge reservation. No real Board task or
Strata generation, Android source or device mutation is performed.
"""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.bridge import EmulatorClient
from strata_board_harness.coordination import Identity,Journal
from strata_board_harness.engine import command_results
from strata_board_harness.gateway import JobGateway
from strata_board_harness.managed_engine import ManagedEngine,process_stamp,group_members
from strata_board_harness.runtime import private_json
from strata_board_harness.supervisor import Store


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='managed-engine-',dir=Path.home()/'.local/share/strata-board-harness-setup')).resolve()
    bridge=EmulatorClient(private_json(Path.home()/'.config/strata-board-harness/mac-bridge.json'),root/'bridge')
    if bridge.guard() is not True:raise RuntimeError('Cross-host reservation not verified')
    requests=[];coding=[0];denials=[0];tool_started=threading.Event();tool_release=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
            command=None;tool_name='bash';tool_arguments=None;answer='Owned fixture title'
            if body.get('tools'):
                coding[0]+=1
                encoded=json.dumps(body);match=re.findall(r'GUIDANCE_ACK (msg_[A-Za-z0-9_-]+)',encoded)
                if 'HARNESS_NEGATIVE_FIXTURE' in encoded:
                    denials[0]+=1
                    if denials[0]==1:command='touch '+str(root/'forbidden-effect')
                    elif denials[0]==2:
                        tool_name='read';tool_arguments={'filePath':str(root/'outside-fixture.txt')}
                    else:answer='Owned forbidden command and outside read rejected.'
                elif match:answer='GUIDANCE_ACK '+match[-1]
                elif coding[0]==1:command='ls'
                elif coding[0]==2:command='harness-tool build'
                else:answer='Owned build failed with exit37; stopped truthfully.'
            if command or tool_arguments:
                delta={'role':'assistant','tool_calls':[{'index':0,'id':'call_'+str(len(requests)),
                    'type':'function','function':{'name':tool_name,'arguments':json.dumps(tool_arguments or {'command':command,'description':'Owned gateway fixture','timeout':30000})}}]};finish='tool_calls'
            else:delta={'role':'assistant','content':answer};finish='stop'
            frames=[{'id':'chatcmpl_fixture','object':'chat.completion.chunk','created':int(time.time()),'model':'fixture',
                'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                {'id':'chatcmpl_fixture','object':'chat.completion.chunk','created':int(time.time()),'model':'fixture',
                'choices':[{'index':0,'delta':{},'finish_reason':finish}],
                'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}]
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for frame in frames:self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
        def log_message(self,*_):pass
    model=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=model.serve_forever,daemon=True);thread.start()
    def build(argument,cancelled):
        tool_started.set()
        deadline=time.monotonic()+20
        while not tool_release.wait(.05):
            if cancelled.is_set() or time.monotonic()>deadline:break
        return {'exit_code':37,'output':'BUILD FAILED: full owned exit37 diagnostic','cleanup_verified':True}
    gateway=JobGateway(root/'jobs',{'build':build})
    os.environ['HARNESS_GATEWAY_URL']=gateway.start();os.environ['HARNESS_GATEWAY_TOKEN']=gateway.token
    work=root/'workspace';work.mkdir();(work/'README.md').write_text('Owned engine fixture only')
    (root/'outside-fixture.txt').write_text('Owned outside sentinel must not reach the model')
    tools=root/'tools';tools.mkdir()
    installed=Path.home()/'.local/share/strata-board-harness/tools'
    (tools/'rtk').symlink_to(installed/'rtk')
    shutil.copyfile(Path(__file__).with_name('harness_tool.py'),tools/'harness-tool');(tools/'harness-tool').chmod(0o700)
    store=Store(root/'state');journal=Journal(store,Identity('fixture','worker','device','fixture-session'))
    settings={'engine_root':str(root/'engine'),'workspace':str(work),'binary':str(installed/'opencode'),
        'tools_dir':str(tools),'worker_lock':str(Path.home()/'.local/share/opencode-worker.lock'),
        'ticket_id':'selected-fixture','provider':'fixture','model':'fixture','step_budget':10,
        'opencode_config':{'model':'fixture/fixture','small_model':'fixture/fixture','provider':{'fixture':{
            'npm':'@ai-sdk/openai-compatible','name':'Owned fake model','options':{'baseURL':f'http://127.0.0.1:{model.server_port}/v1','apiKey':'fixture-only'},
            'models':{'fixture':{'name':'Fixture','limit':{'context':32768,'output':2048}}}}},
            'agent':{'strata-harness':{'mode':'primary','steps':10}},
            'permission':{'*':'deny','read':'allow','bash':{'*':'deny','ls':'allow','rtk ls':'allow','harness-tool *':'allow'},'external_directory':'deny'}}}
    engine=ManagedEngine(store,journal,settings,bridge.guard,gateway.clean,lambda *_:True)
    receipt={'real_strata_requests':0,'real_board_tasks':0,'temporary_root':str(root),'passed':False}
    try:
        deadline=time.monotonic()+90;errors=[]
        while time.monotonic()<deadline:
            try:
                if engine.prepare():break
            except RuntimeError as error:
                errors.append(type(error).__name__+':'+str(error))
                if engine.state.get('phase') in ('failed','reconciliation_required'):raise
            time.sleep(.2)
        else:raise RuntimeError('Managed session readiness deadline')
        receipt['startup_errors']=errors;receipt['session_id']=engine.state['session_id']
        engine.start_prompt('HARNESS_MANAGED_FIXTURE: run ls, then harness-tool build, report failure and stop.')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not tool_started.is_set():engine.observe_history();time.sleep(.1)
        if not tool_started.is_set():raise RuntimeError('Actual scoped build command not observed')
        before=coding[0]
        body='Check scoped guidance at the next safe boundary.'
        import hashlib
        message={'id':'owned-ping','thread_id':'fixture-thread','author':{'kind':'agent','owner_id':'fixture','agent_id':'manager'}}
        journal.persist_page(None,{'session':{'id':'fixture-session','owner_id':'fixture','agent_id':'worker','device_id':'device'},'messages':[message],'cursor':'fixture-cursor'})
        journal.save_body('owned-ping',body,hashlib.sha256(body.encode()).hexdigest())
        engine.session.deliver('owned-ping');time.sleep(.2)
        receipt['no_model_step_during_active_tool']=coding[0]==before
        tool_release.set()
        deadline=time.monotonic()+30;ack=False;failed=False;rtk=False
        while time.monotonic()<deadline:
            latest=engine.observe_history()
            for (raw,) in journal.db.execute('SELECT body FROM engine_messages'):
                value=json.loads(raw)
                for result in command_results(value):
                    rtk=rtk or result['command']=='rtk ls'
                    failed=failed or result['exit_code']==37 and result['passed'] is False
                if value.get('info',{}).get('role')=='assistant' and value['info'].get('parentID')==journal._message('owned-ping')['request_id']:
                    try:engine.session.acknowledge('owned-ping',value['info']['id']);ack=True
                    except RuntimeError:pass
            if ack and failed and rtk:break
            time.sleep(.1)
        receipt.update(rtk_actual_command=rtk,real_exit37=failed,assistant_ack=ack,steps=engine.state['steps'],token_totals=engine.state.get('token_totals'))
        receipt['passed']=all((rtk,failed,ack,receipt['no_model_step_during_active_tool']))
        receipt['build_timeout_raised']=any(
            part.get('tool')=='bash' and part.get('state',{}).get('input',{}).get('command')=='harness-tool build'
            and part.get('state',{}).get('input',{}).get('timeout')==920000
            for (raw,) in journal.db.execute('SELECT body FROM engine_messages') for part in json.loads(raw).get('parts',[]))
        engine.session.client.request('POST',engine.session._path('/prompt_async'),
            {'parts':[{'type':'text','text':'HARNESS_NEGATIVE_FIXTURE: exercise only the owned rejected fixtures, then stop.'}],
             'agent':'strata-harness','model':{'providerID':'fixture','modelID':'fixture'}})
        deadline=time.monotonic()+30;rejected=set()
        while time.monotonic()<deadline:
            engine.observe_history()
            for (raw,) in journal.db.execute('SELECT body FROM engine_messages'):
                for part in json.loads(raw).get('parts',[]):
                    state=part.get('state',{});entry=state.get('input',{})
                    if state.get('status')=='error' and entry.get('command','').startswith('touch '):rejected.add('command')
                    if state.get('status')=='error' and entry.get('filePath')==str(root/'outside-fixture.txt'):rejected.add('read')
            if len(rejected)==2:break
            time.sleep(.1)
        receipt['outside_command_rejected']='command' in rejected and not (root/'forbidden-effect').exists()
        receipt['outside_native_read_rejected']='read' in rejected
        receipt['passed']=all((receipt['passed'],receipt['build_timeout_raised'],receipt['outside_command_rejected'],receipt['outside_native_read_rejected']))
        # Seed user-only history through the actual noReply API so pagination
        # crosses its page boundary without coding or generation side effects.
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            status,states=engine.client.request('GET','/session/status')
            if status==200 and (engine.state['session_id'] not in states or states[engine.state['session_id']].get('type')=='idle'):break
            time.sleep(.1)
        else:raise RuntimeError('Fixture session did not become idle before history probe')
        before_requests=len(requests);seeded=[]
        import secrets
        for i in range(25):
            identifier='msg_history_'+secrets.token_hex(12)
            status,value=engine.client.request('POST',engine.session._path('/message'),{
                'messageID':identifier,'noReply':True,'agent':'strata-harness',
                'model':{'providerID':'fixture','modelID':'fixture'},
                'parts':[{'type':'text','text':'Owned no-generation history fixture '+str(i)}]})
            if status!=200 or value.get('info',{}).get('id')!=identifier:raise RuntimeError('No-reply history seeding failed')
            seeded.append(identifier)
        engine.observe_history()
        receipt['actual_paginated_history_complete']=all(journal.db.execute('SELECT 1 FROM engine_messages WHERE id=?',(i,)).fetchone() for i in seeded)
        receipt['history_seed_without_model_requests']=len(requests)==before_requests
        receipt['passed']=receipt['passed'] and receipt['actual_paginated_history_complete'] and receipt['history_seed_without_model_requests']
        original_session=engine.state['session_id'];original_steps=engine.state['steps']
        original_pid=engine.state['process']['pid']
        if not engine.stop('explicit_fake_restart_preflight'):raise RuntimeError('Preflight restart cleanup failed')
        before=len(requests);engine.state.update(phase='new',cleanup_verified=False,stop_reason=None)
        engine.save();deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if engine.prepare():break
            time.sleep(.1)
        else:raise RuntimeError('Actual reattachment readiness deadline')
        receipt['reattach_same_session']=engine.state['session_id']==original_session
        receipt['reattach_preserved_steps']=engine.state['steps']==original_steps
        receipt['reattach_new_owned_pid']=engine.state['process']['pid']!=original_pid
        receipt['reattach_without_model_request']=len(requests)==before
        receipt['passed']=receipt['passed'] and all(receipt[k] for k in (
            'reattach_same_session','reattach_preserved_steps','reattach_new_owned_pid','reattach_without_model_request'))
    except Exception as error:
        receipt['error']={'type':type(error).__name__,'message':str(error)}
    finally:
        tool_release.set();gateway.stop(timeout=5)
        receipt['process_cleanup_verified']=engine.stop('owned_preflight_finished')
        receipt['worker_lock_absent']=not Path(settings['worker_lock']).exists()
        model.shutdown();model.server_close();thread.join(timeout=2);store.close()
        receipt['model_requests']=len(requests)
        if receipt['process_cleanup_verified'] and receipt['worker_lock_absent']:
            shutil.rmtree(root);receipt['temporary_files_removed']=not root.exists()
        output.write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt,indent=2))
    return 0 if receipt['passed'] and receipt['process_cleanup_verified'] else 1


if __name__=='__main__':raise SystemExit(main())
