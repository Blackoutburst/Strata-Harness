"""Install a verified distribution bubblewrap binary into the user's tools.

No apt install, root changes or unrelated service modifications. Package digest
comes from the host's configured Ubuntu package metadata.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    setup=Path.home()/'.local/share/strata-board-harness-setup';setup.mkdir(exist_ok=True,parents=True,mode=0o700)
    tools=Path.home()/'.local/share/strata-board-harness/tools';tools.mkdir(exist_ok=True,parents=True)
    destination=tools/'bubblewrap'
    if destination.exists():raise RuntimeError('Existing sandbox tooling: inspect before replacement')
    metadata=subprocess.check_output(['apt-cache','show','bubblewrap'],text=True).split('\n\n')[0]
    fields=dict(line.split(': ',1) for line in metadata.splitlines() if ': ' in line)
    with tempfile.TemporaryDirectory(prefix='sandbox-package-',dir=setup) as tmp:
        subprocess.run(['apt-get','download','bubblewrap='+fields['Version']],cwd=tmp,check=True,capture_output=True,timeout=60)
        files=list(Path(tmp).glob('*.deb'))
        if len(files)!=1:raise RuntimeError('Expected one package')
        digest=hashlib.sha256(files[0].read_bytes()).hexdigest()
        if digest!=fields['SHA256']:raise RuntimeError('Distribution package hash mismatch')
        subprocess.run(['dpkg-deb','-x',str(files[0]),str(Path(tmp)/'unpacked')],check=True,timeout=10)
        import shutil
        shutil.copytree(Path(tmp)/'unpacked',destination)
    binary=destination/'usr/bin/bwrap'
    result=subprocess.run([str(binary),'--unshare-all','--die-with-parent','--new-session',
        '--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/lib','/lib',
        '--symlink','usr/lib64','/lib64','--proc','/proc','--dev','/dev',
        '--tmpfs','/tmp','--dir','/home','--','/bin/sh','-c','test ! -e /home/ubuntu/.config/strata-board-harness/board.json && test ! -e /home/ubuntu/.ssh && echo isolated'],
        capture_output=True,text=True,timeout=10)
    receipt={'package':fields['Package'],'version':fields['Version'],'package_sha256':digest,
        'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'probe_exit':result.returncode,
        'probe_stdout':result.stdout,'probe_stderr':result.stderr}
    (setup/'build-sandbox.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt,indent=2))
    if result.returncode:raise SystemExit(result.returncode)


if __name__=='__main__':main()
