"""Opt-in PC companion for file-bound search hints published by Flix."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def fresh_file(path):
    try:
        if time.time() - path.stat().st_mtime > 900:
            return None
        with path.open(encoding='utf8') as stream:
            row = json.loads(stream.read(4097))
        if not isinstance(row, dict) or row.get('schema') != 1:
            return None
        digest = row.get('file_id')
        if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
            return digest
    except (OSError, ValueError):
        pass
    return None


def check_once(path, attempted, command):
    digest = fresh_file(path)
    if digest is None or digest in attempted:
        return False
    attempted.append(digest)
    try:
        subprocess.run([*command, '--expected-file', digest], timeout=90, check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        print('Audio scan could not complete for this file.', file=sys.stderr, flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path(os.environ.get('APPDATA', '')) /
                        'Kodi/userdata/addon_data/plugin.video.mdm.flix')
    parser.add_argument('--reference-db', type=Path, action='append', default=[])
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--catalogue', type=Path)
    source.add_argument('--public-catalogue', action='store_true')
    parser.add_argument('--fpcalc', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kodi-preview', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    catalogue_mode = args.catalogue or args.public_catalogue
    if catalogue_mode and (args.reference_db or args.kodi_preview
                           or (args.catalogue and not args.catalogue.is_file())):
        parser.error('Supply an existing catalogue without reference or preview options')
    if not catalogue_mode and (not 1 <= len(args.reference_db) <= 3
                               or any(not path.is_file() for path in args.reference_db)):
        parser.error('Supply one to three existing reference databases or a catalogue')
    command = [sys.executable, str(Path(__file__).with_name('kodi_live.py')), '--bridge',
               '--profile', str(args.profile), '--fpcalc', str(args.fpcalc), '--output', str(args.output)]
    if args.catalogue:
        command.extend(('--catalogue', str(args.catalogue)))
    elif args.public_catalogue:
        command.append('--public-catalogue')
    else:
        command.extend(('--blind-windows', '--auto-reference'))
    for path in args.reference_db:
        command.extend(('--reference-db', str(path)))
    if args.kodi_preview:
        command.append('--kodi-preview')
    attempted = deque(maxlen=128)
    path = args.profile / ('shared_timing_request.json' if catalogue_mode else 'audio_search_hint.json')
    try:
        while True:
            check_once(path, attempted, command)
            if args.once:
                return
            time.sleep(3)
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    main()
