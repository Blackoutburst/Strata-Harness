"""Install pinned official engine/RTK assets into this user's harness directory."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen


ASSETS = [
    ('opencode', 'https://github.com/anomalyco/opencode/releases/download/v1.19.3/opencode-linux-x64-baseline.tar.gz',
     '531cff2441129c30f0edd5878af494108ad07f5feb46d7ee29a2a5b830a5765d'),
    ('rtk', 'https://github.com/rtk-ai/rtk/releases/download/v1.0.1/rtk-x86_64-unknown-linux-musl.tar.gz',
     '5b919c3bccb799b67a61866d0e617410a4fcdf173e2614440e59de2fa2003436'),
]


def install(directory):
    if subprocess.check_output(['uname', '-sm'], text=True).strip() != 'Linux x86_64':
        raise RuntimeError('These pinned assets are Linux x86_64 only')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = {'checked_at': datetime.now(timezone.utc).isoformat(), 'assets': []}
    with tempfile.TemporaryDirectory(prefix='strata-tools-') as temporary:
        for name, url, digest in ASSETS:
            target = directory / name
            if target.exists(): raise RuntimeError('Existing tool requires explicit version reconciliation: ' + name)
            archive = Path(temporary) / (name + '.tar.gz')
            with urlopen(url, timeout=30) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Official asset digest mismatch: ' + name)
            with tarfile.open(archive, 'r:gz') as source:
                members = [member for member in source.getmembers()
                           if member.isfile() and Path(member.name).name == name]
                if len(members) != 1: raise RuntimeError('Unexpected official archive layout')
                with source.extractfile(members[0]) as input_file, target.open('xb') as output:
                    shutil.copyfileobj(input_file, output)
            target.chmod(0o700)
            version = subprocess.check_output([str(target), '--version'], text=True, timeout=10).strip()
            receipt['assets'].append({'name': name, 'url': url, 'archive_sha256': digest,
                'binary_sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'version': version})
            print(name + ': ' + version, flush=True)
    (directory / 'installed.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path,
        default=Path.home() / '.local/share/strata-board-harness/tools')
    install(parser.parse_args().directory)
