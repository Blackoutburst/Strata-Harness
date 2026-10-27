"""Prepare only the baseline-required SDK in the harness's private tools.

Google's repository metadata selects the exact Linux archive/checksum. Existing
accepted licenses/platform files are copied; the shared SDK is never changed.
"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import urlopen
import xml.etree.ElementTree as ET
import zipfile
import json


def main():
    root=Path.home()/'.local/share/strata-board-harness/tools/android-sdk'
    if root.exists():raise RuntimeError('Existing owned SDK: reconcile instead of replacing')
    source=Path.home()/'Android/Sdk'
    with urlopen('https://dl.google.com/android/repository/repository2-1.xml',timeout=20) as response:
        metadata=response.read(16*1024*1024)
    tree=ET.fromstring(metadata)
    package=next(e for e in tree.iter() if e.tag.split('}')[-1]=='remotePackage' and e.get('path')=='build-tools;36.0.0')
    def child(element,name):return next(e for e in element if e.tag.split('}')[-1]==name)
    archives=child(package,'archives')
    archive=next(e for e in archives if child(e,'host-os').text=='linux')
    complete=child(archive,'complete');url='https://dl.google.com/android/repository/'+child(complete,'url').text
    expected=child(complete,'checksum').text
    with tempfile.TemporaryDirectory(prefix='android-sdk-',dir=root.parent) as tmp:
        path=Path(tmp)/'build-tools.zip'
        with urlopen(url,timeout=60) as response,path.open('wb') as output:shutil.copyfileobj(response,output)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if hashlib.sha1(path.read_bytes()).hexdigest()!=expected:raise RuntimeError('Google SDK archive checksum mismatch')
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                name=Path(item.filename)
                if name.is_absolute() or '..' in name.parts or ((item.external_attr>>16)&0o170000)==0o120000:
                    raise RuntimeError('Unsafe SDK archive member')
            archive.extractall(Path(tmp)/'unpacked')
            for item in archive.infolist():
                mode=(item.external_attr>>16)&0o777
                dest=Path(tmp)/'unpacked'/item.filename
                if mode and dest.is_file():dest.chmod(mode)
        roots=list((Path(tmp)/'unpacked').iterdir())
        if len(roots)!=1 or not (roots[0]/'aapt2').is_file():raise RuntimeError('Unexpected SDK archive structure')
        root.mkdir(mode=0o700);(root/'build-tools').mkdir();(root/'platforms').mkdir()
        shutil.move(str(roots[0]),root/'build-tools/36.0.0')
    for name in ('platforms/android-36','licenses'):
        target=root/name
        subprocess.run(['cp','--reflink=auto','-a',str(source/name),str(target)],check=True,timeout=60)
    result=subprocess.run([str(root/'build-tools/36.0.0/aapt2'),'version'],capture_output=True,text=True,timeout=10)
    receipt={'url':url,'google_archive_sha1':expected,'archive_sha256':digest,'baseline_compile_sdk':36,
             'build_tools':'36.0.0','exit_code':result.returncode,'version':result.stdout+result.stderr}
    (Path.home()/'.local/share/strata-board-harness-setup/android-sdk.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt,indent=2))
    if result.returncode:raise SystemExit(result.returncode)


if __name__=='__main__':main()
