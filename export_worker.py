"""Build immutable public-only releases and atomically select the latest one."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile

from review_workflow import bundle, report, validate_bundle

RELEASE=re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}')


def export(data,root):
    data,root=Path(data),Path(root)
    root.mkdir(parents=True,exist_ok=True)
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(6)
    destination=root/name
    manifest=bundle(data,destination)
    validate_bundle(destination)
    # The service's primary group owns these files; no setgid permission is needed.
    os.chmod(destination,0o750)
    for path in destination.iterdir():
        os.chmod(path,0o640)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='ascii',dir=root,delete=False) as stream:
            temporary=Path(stream.name)
            stream.write(name+'\n')
        os.chmod(temporary,0o640)
        os.replace(temporary,root/'current-export')
        temporary=None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    # The moderator report remains private and is never read by the publisher.
    private=data/'review-report.json'
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=data,delete=False) as stream:
        private_temp=Path(stream.name)
        json.dump(report(data),stream,sort_keys=True,indent=2,allow_nan=False)
    try:
        os.chmod(private_temp,0o600)
        os.replace(private_temp,private)
    finally:
        private_temp.unlink(missing_ok=True)
    existing=sorted((p for p in root.iterdir() if RELEASE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),key=lambda p:p.name,reverse=True)
    for old in existing[20:]:
        if old!=destination and old.resolve().parent==root.resolve():
            shutil.rmtree(old)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(export(args.data,args.output),sort_keys=True))


if __name__=='__main__':
    main()
