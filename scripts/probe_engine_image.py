"""One real Strata/OpenCode/RTK image-path smoke in a disposable workspace."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from strata_board_harness.bridge import EmulatorClient
from strata_board_harness.coordination import Identity,Journal
from strata_board_harness.engine import command_results
from strata_board_harness.managed_engine import ManagedEngine
from strata_board_harness.model_proxy import ModelProxy
from strata_board_harness.runtime import private_json
from strata_board_harness.supervisor import Store


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);a=parser.parse_args()
    setup=Path.home()/'.local/share/strata-board-harness-setup'
    root=Path(tempfile.mkdtemp(prefix='engine-image-',dir=setup)).resolve()
    image=json.loads((setup/'image-fixture.json').read_text())
    work=root/'workspace';work.mkdir();(work/'fixture.png').write_bytes(base64.b64decode(image['base64']))
    store=Store(root/'state');journal=Journal(store,Identity('fixture','worker','device','image-fixture-session'))
    installed=Path.home()/'.local/share/strata-board-harness/tools'
    bridge=EmulatorClient(private_json(Path.home()/'.config/strata-board-harness/mac-bridge.json'),root/'bridge')
    if bridge.guard() is not True:raise RuntimeError('Cross-host reservation not verified')
    proxy=ModelProxy('http://127.0.0.1:18080/v1',
        (Path.home()/'.config/strata/api-key').read_text().strip(),'swift-1.5-iq3_s',root/'model-proxy')
    endpoint=proxy.start();os.environ['STRATA_MODEL_PROXY_TOKEN']=proxy.token
    settings={'engine_root':str(root/'engine'),'workspace':str(work),'binary':str(installed/'opencode'),
        'tools_dir':str(installed),'worker_lock':str(Path.home()/'.local/share/opencode-worker.lock'),
        'ticket_id':'selected-image-smoke','provider':'strata','model':'swift-1.5-iq3_s','step_budget':8,
        'opencode_config':{'model':'strata/swift-1.5-iq3_s','small_model':'strata/swift-1.5-iq3_s',
            'provider':{'strata':{'npm':'@ai-sdk/openai-compatible','name':'Strata',
                'options':{'baseURL':endpoint,'apiKey':'{env:STRATA_MODEL_PROXY_TOKEN}'},
                'models':{'swift-1.5-iq3_s':{'name':'Swift IQ3_S','attachment':True,'tool_call':True,
                    'modalities':{'input':['text','image'],'output':['text']},'limit':{'context':262144,'output':1024},
                    'options':{'chat_template_kwargs':{'enable_thinking':False}}}}}},
            'agent':{'strata-harness':{'mode':'primary','steps':8}},
            'permission':{'*':'deny','read':{'*':'allow'},'bash':{'*':'deny','ls':'allow','rtk ls':'allow'},'external_directory':'deny'}}}
    engine=ManagedEngine(store,journal,settings,bridge.guard,lambda:True,lambda *_:False)
    receipt={'passed':False,'real_board_tasks':0,'kind':'infrastructure_image_path_smoke','request_settings':{
        'model':'strata/swift-1.5-iq3_s','context_limit':262144,'output_limit':1024,
        'configured_options':{'chat_template_kwargs':{'enable_thinking':False}},'steps':8}}
    try:
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            try:
                if engine.prepare():break
            except RuntimeError:
                if engine.state['phase'] in ('failed','reconciliation_required'):raise
            time.sleep(.2)
        else:raise RuntimeError('Engine readiness deadline')
        engine.start_prompt('Infrastructure smoke only. Use the read tool to inspect the actual file fixture.png in this workspace. Run ls once through bash. Then reply with only the six digit number visible in the image and stop. Do not edit or create files, call any other command, or inspect other directories.')
        deadline=time.monotonic()+180;read=False;rtk=False;recognized=False
        while time.monotonic()<deadline:
            engine.observe_history()
            for (raw,) in journal.db.execute('SELECT body FROM engine_messages'):
                value=json.loads(raw)
                for part in value.get('parts',[]):
                    state=part.get('state',{})
                    if part.get('type')=='tool' and part.get('tool')=='read' and state.get('status')=='completed':
                        read=read or str(state.get('input',{}).get('filePath','')).endswith('fixture.png')
                    if part.get('type')=='text' and re.search(r'(?<!\d)'+image['expected']+r'(?!\d)',part.get('text','')):recognized=True
                rtk=rtk or any(r['command']=='rtk ls' and r['passed'] for r in command_results(value))
            if read and rtk and recognized:break
            time.sleep(.2)
        receipt.update(read_actual_image_path=read,rtk_actual_ls=rtk,known_code_recognized=recognized,
            session_id=engine.state['session_id'],steps=engine.state['steps'],token_totals=engine.state.get('token_totals'),
            api_errors=engine.state.get('api_errors'),passed=read and rtk and recognized)
        (setup/'engine-image-history.json').write_text(json.dumps([json.loads(row[0]) for row in journal.db.execute('SELECT body FROM engine_messages')],indent=2))
    except Exception as error:receipt['error']={'type':type(error).__name__,'message':str(error)}
    finally:
        receipt['process_cleanup_verified']=engine.stop('image_smoke_finished')
        receipt['worker_lock_absent']=not Path(settings['worker_lock']).exists()
        proxy.close()
        proxy_receipts=[json.loads(line) for line in proxy.log.read_text().splitlines()] if proxy.log.exists() else []
        receipt['model_requests']=[v for v in proxy_receipts if v.get('state')!='prepared']
        receipt['upstream_usage_recorded']=bool(receipt['model_requests']) and all(v.get('usage') for v in receipt['model_requests'] if v['state']=='completed')
        receipt['thinking_disabled_on_wire']=bool(receipt['model_requests']) and all(v.get('requested_settings',{}).get('chat_template_kwargs',{}).get('enable_thinking') is False for v in receipt['model_requests'])
        receipt['passed']=receipt['passed'] and receipt['upstream_usage_recorded'] and receipt['thinking_disabled_on_wire']
        store.close();os.environ.pop('STRATA_MODEL_PROXY_TOKEN',None)
        if receipt['process_cleanup_verified'] and receipt['worker_lock_absent']:shutil.rmtree(root);receipt['temporary_files_removed']=True
        Path(a.output).write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt,indent=2))
    return 0 if receipt['passed'] and receipt['process_cleanup_verified'] else 1


if __name__=='__main__':raise SystemExit(main())
