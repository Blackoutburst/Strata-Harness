"""Authenticated, read-only metadata + bounded tool/image inference preflight.

No configuration, model lifecycle or SSH operations. Credentials remain in the
owning host's private credential file and never appear in receipts.
"""
import argparse
import base64
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,build_opener,HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def main():
    p=argparse.ArgumentParser();p.add_argument('--image-fixture',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    key=(Path.home()/'.config/strata/api-key').read_text().strip()
    base='http://127.0.0.1:18080'
    def request(method,path,payload=None,timeout=15):
        r=Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,method=method,
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        started=time.monotonic()
        try:
            with build_opener(NoRedirect()).open(r,timeout=timeout) as response:
                body=response.read(4*1024*1024+1)
                if len(body)>4*1024*1024:raise RuntimeError('Oversized preflight response')
                return {'http_status':response.status,'seconds':time.monotonic()-started,'body':json.loads(body)}
        except HTTPError as error:
            status=error.code;body=error.read(4096).decode(errors='replace');error.close()
            return {'http_status':status,'seconds':time.monotonic()-started,'error_body':body}
        except (OSError,URLError,ValueError):return {'http_status':None,'seconds':time.monotonic()-started,'error':'unavailable_or_invalid'}
    result={'at':datetime.now(timezone.utc).isoformat(),'model':'swift-1.5-iq3_s',
        'settings':{'temperature':0,'max_tokens':256,'stream':False,'chat_template_kwargs':{'enable_thinking':False}},
        'configured_api_billing_usd':0,'energy':'unmeasured'}
    result['models']=request('GET','/v1/models')
    properties=request('GET','/props')
    # Metadata allowlist: never write an unknown server configuration blob.
    if properties.get('http_status')==200:
        body=properties['body'];keys=('model_path','model_file','model_alias','model','n_ctx','engine_version','vision','vision_enabled','chat_template_caps')
        result['properties']={k:body[k] for k in keys if k in body}
        for name in ('default_generation_settings','meta'):
            value=body.get(name)
            if isinstance(value,dict):result['properties'][name]={k:value[k] for k in ('n_ctx','model','model_path','temperature','top_p') if k in value}
    else:result['properties_status']=properties.get('http_status')
    nonce=secrets.token_hex(8)
    payload={'model':result['model'],**result['settings'],'messages':[{'role':'user','content':'Call harness_ping with nonce '+nonce+'. Do not answer in prose.'}],
        'tools':[{'type':'function','function':{'name':'harness_ping','description':'Return the exact supplied nonce for a harmless tool-path check',
            'parameters':{'type':'object','properties':{'nonce':{'type':'string'}},'required':['nonce'],'additionalProperties':False}}}],
        'tool_choice':'auto'}
    tool=request('POST','/v1/chat/completions',payload,timeout=120);result['tool_probe']=tool
    try:
        call=tool['body']['choices'][0]['message']['tool_calls'][0]
        result['tool_call_verified']=call['function']['name']=='harness_ping' and json.loads(call['function']['arguments'])=={'nonce':nonce}
    except (KeyError,IndexError,TypeError,ValueError):result['tool_call_verified']=False
    fixture=json.loads(Path(a.image_fixture).read_text())
    image=base64.b64decode(fixture['base64'],validate=True)
    payload={'model':result['model'],**result['settings'],'messages':[{'role':'user','content':[
        {'type':'text','text':'Read the six digit number shown in this image. Reply with only those six digits.'},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+fixture['base64']}}]}]}
    response=request('POST','/v1/chat/completions',payload,timeout=120);result['image_probe']=response
    result['image_fixture_sha256']=hashlib.sha256(image).hexdigest()
    try:
        answer=response['body']['choices'][0]['message']['content'].strip()
        result['image_verified']=answer==fixture['expected']
    except (KeyError,IndexError,TypeError,AttributeError):result['image_verified']=False
    output=Path(a.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2));output.chmod(0o600)
    print(json.dumps({k:result[k] for k in ('at','model','properties','tool_call_verified','image_verified') if k in result},indent=2))


if __name__=='__main__':main()
