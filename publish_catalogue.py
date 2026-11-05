"""Publish only the four validated public files using a repository-specific deploy key."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from export_worker import RELEASE
from review_workflow import validate_bundle

REMOTE='git@github.com:MDMAinsley/mdm-timing-catalogue.git'
PUBLIC=('playback-snapshot.json','playback-snapshot-v3.json','review-queue.json','catalogue-release.json')


def git(path,*args):
    command=['git']
    if path:
        command.extend(['-C',str(path)])
    result=subprocess.run(command+list(args),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if result.returncode:
        # Do not print environment values or authentication material.
        raise RuntimeError('catalogue_git_'+args[0]+'_failed')
    return result.stdout.strip()


def publish(release,checkout):
    release,checkout=Path(release),Path(checkout)
    manifest=validate_bundle(release)
    # Read bytes once before touching Git, so later file changes cannot race validation.
    content={name:(release/name).read_bytes() for name in PUBLIC}
    for name in ('playback-snapshot.json','playback-snapshot-v3.json','review-queue.json'):
        if hashlib.sha256(content[name]).hexdigest()!=manifest['files'][name]:
            raise ValueError('bundle_changed_during_publication')
    import json
    if json.loads(content['catalogue-release.json'])!=manifest:
        raise ValueError('manifest_changed_during_publication')
    if not (checkout/'.git').exists():
        if checkout.exists() and any(checkout.iterdir()):
            raise RuntimeError('publisher_checkout_not_empty')
        git(None,'-c','core.autocrlf=false','clone','--branch','main',REMOTE,str(checkout))
    git(checkout,'config','core.hooksPath','/dev/null' if os.name!='nt' else 'NUL')
    git(checkout,'config','core.autocrlf','false')
    git(checkout,'config','user.name','MDM Skip Publisher')
    git(checkout,'config','user.email','noreply@mdmainsley.co.uk')
    if git(checkout,'remote','get-url','origin')!=REMOTE:
        raise RuntimeError('unexpected_publication_repository')
    if git(checkout,'status','--porcelain'):
        raise RuntimeError('publisher_checkout_needs_review')
    git(checkout,'fetch','origin','main')
    # Stop rather than rewriting or discarding unpublished history on divergence.
    git(checkout,'merge','--ff-only','origin/main')
    for name,raw in content.items():
        if (checkout/name).is_symlink():
            raise ValueError('unsafe_publication_file')
        (checkout/name).write_bytes(raw)
    git(checkout,'add','--',*PUBLIC)
    for name,raw in content.items():
        staged=subprocess.run(['git','-C',str(checkout),'show',':'+name],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
        if staged!=raw:
            raise RuntimeError('git_transformed_publication_bytes')
    changed=git(checkout,'diff','--cached','--name-only').splitlines()
    if any(name not in PUBLIC for name in changed):
        raise RuntimeError('unexpected_publication_paths')
    if changed:
        git(checkout,'commit','-m','Publish reviewed MDM Skip catalogue')
    git(checkout,'push','origin','HEAD:refs/heads/main')
    return dict(changed=bool(changed),commit=git(checkout,'rev-parse','HEAD'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exports',type=Path,required=True)
    parser.add_argument('--checkout',type=Path,required=True)
    args=parser.parse_args()
    name=(args.exports/'current-export').read_text(encoding='ascii').strip()
    if not RELEASE.fullmatch(name):
        raise ValueError('invalid_export_pointer')
    root=args.exports.resolve()
    release=args.exports/name
    if release.is_symlink() or release.resolve().parent!=root:
        raise ValueError('unsafe_export_path')
    import json
    print(json.dumps(publish(release,args.checkout),sort_keys=True))


if __name__=='__main__':
    main()
