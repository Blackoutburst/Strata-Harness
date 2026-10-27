"""Complete the existing Ubuntu JRE with its matched JDK package, privately."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile


def main():
    setup=Path.home()/'.local/share/strata-board-harness-setup';tools=Path.home()/'.local/share/strata-board-harness/tools'
    destination=tools/'jdk17'
    if destination.exists():raise RuntimeError('Existing owned JDK: inspect before replacement')
    metadata=subprocess.check_output(['apt-cache','show','openjdk-17-jdk-headless'],text=True).split('\n\n')[0]
    fields=dict(line.split(': ',1) for line in metadata.splitlines() if ': ' in line)
    with tempfile.TemporaryDirectory(prefix='jdk-package-',dir=setup) as tmp:
        subprocess.run(['apt-get','download','openjdk-17-jdk-headless='+fields['Version']],cwd=tmp,check=True,capture_output=True,timeout=120)
        files=list(Path(tmp).glob('*.deb'))
        if len(files)!=1:raise RuntimeError('Expected one JDK package')
        digest=hashlib.sha256(files[0].read_bytes()).hexdigest()
        if digest!=fields['SHA256']:raise RuntimeError('Distribution JDK hash mismatch')
        subprocess.run(['dpkg-deb','-x',str(files[0]),str(Path(tmp)/'unpacked')],check=True,timeout=20)
        subprocess.run(['cp','--reflink=auto','-a','/usr/lib/jvm/java-17-openjdk-amd64',str(destination)],check=True,timeout=30)
        shutil.copytree(Path(tmp)/'unpacked/usr/lib/jvm/java-17-openjdk-amd64',destination,dirs_exist_ok=True,symlinks=True)
    result=subprocess.run([str(destination/'bin/javac'),'-version'],capture_output=True,text=True,timeout=15)
    receipt={'package':fields['Package'],'version':fields['Version'],'package_sha256':digest,
             'javac_exit':result.returncode,'javac':result.stdout+result.stderr}
    (setup/'jdk17.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt,indent=2))
    if result.returncode:raise SystemExit(result.returncode)


if __name__=='__main__':main()
