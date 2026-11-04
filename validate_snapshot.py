"""Validate the public playback payload and reject extra contribution fields."""
import argparse
import json
from pathlib import Path
import re

from live_catalogue import load_snapshot


def validate(path):
    cuts, markers = load_snapshot(path)
    data = json.loads(path.read_text(encoding='utf8'))
    if set(data) != {'schema', 'cuts'} or data['schema'] != 2:
        raise ValueError('invalid_public_snapshot_fields')
    for key, cut in data['cuts'].items():
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('invalid_public_cut_id')
        if set(cut) != {'episode', 'duration', 'anchors', 'markers'}:
            raise ValueError('invalid_public_cut_fields')
        for marker in cut['markers'].values():
            if set(marker) != {'id', 'revision', 'start', 'end', 'tier'}:
                raise ValueError('invalid_public_marker_fields')
            if not isinstance(marker['id'], str) or not re.fullmatch('[0-9a-f]{64}', marker['id']):
                raise ValueError('invalid_public_marker_id')
            if type(marker['revision']) is not int or marker['revision'] < 1:
                raise ValueError('invalid_public_marker_revision')
    return len(cuts), sum(len(value) for value in markers.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    cuts, markers = validate(args.snapshot)
    print(f'Validated {cuts} cuts and {markers} timing markers.')


if __name__ == '__main__':
    main()
