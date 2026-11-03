"""Read a locally provisioned snapshot and identify a cut from opening audio."""
import hashlib
import json
import math
import struct

from catalogue import (Cut, Proposal, Review, RUNTIME_TOLERANCE, match_cut,
                       write_markers, write_playback_match)
from detector import STEP, informative

METHOD = 'chromaprint2-fixed-32-v1'
POSITIONS = (0, 16)


def opening_anchors(words):
    anchors = []
    for position in POSITIONS:
        first = round(position / STEP)
        block = words[first:first + 32]
        if len(block) == 32 and informative(block):
            anchors.append((METHOD, position, hashlib.sha256(struct.pack('<32I', *block)).hexdigest()))
    return tuple(anchors)


def load_snapshot(path):
    with path.open(encoding='utf8') as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('catalogue_snapshot_too_large')
    data = json.loads(raw)
    schema = data['schema']
    if schema not in (1, 2):
        raise ValueError('catalogue_schema_unsupported')
    cuts = {}
    proposals = {}
    for key, row in data['cuts'].items():
        episode = tuple(row['episode'])
        duration = row['duration']
        if (len(episode) != 3 or any(type(part) is not int for part in episode)
                or episode[0] < 1 or episode[1] < 0 or episode[2] < 1
                or type(duration) not in (int, float) or not math.isfinite(duration)
                or not 60 <= duration <= 86400):
            raise ValueError('catalogue_cut_invalid')
        anchors = tuple(tuple(anchor) for anchor in row['anchors'])
        if any(method != METHOD or position not in POSITIONS or len(digest) != 64
               or any(char not in '0123456789abcdef' for char in digest)
               for method, position, digest in anchors):
            raise ValueError('catalogue_anchor_invalid')
        if len(anchors) != len(POSITIONS) or {anchor[1] for anchor in anchors} != set(POSITIONS):
            raise ValueError('catalogue_anchor_coverage_invalid')
        cuts[key] = Cut(episode, duration, anchors)
        if schema == 2:
            proposals[key] = {}
            for kind, marker in row['markers'].items():
                start, end, tier = (marker[field] for field in ('start', 'end', 'tier'))
                if (kind not in ('intro', 'outro') or not 0 <= start < end <= row['duration']
                        or tier not in ('software generated', 'community reviewed', 'trusted reviewer')):
                    raise ValueError('catalogue_marker_invalid')
                proposals[key][kind] = dict(start=start, end=end, tier=tier)
        else:
            proposals[key] = [Proposal(**proposal) for proposal in row['proposals']]
    if schema == 2:
        return cuts, proposals
    return cuts, proposals, [Review(**review) for review in data['reviews']], set(data['trusted_accounts'])


def episode_key(item):
    return item['tmdb_id'], item['season'], item['episode']


def save_cut_draft(path, item, sample, metrics):
    anchors = opening_anchors(sample.words)
    if metrics['sample_start_seconds'] != 0 or len(anchors) != len(POSITIONS):
        raise ValueError('incomplete_cut_fingerprint')
    draft = dict(episode=episode_key(item), duration=metrics['playback_duration'],
                 anchors=anchors, proposals=[])
    with path.open('x', encoding='utf8') as stream:
        json.dump(draft, stream, indent=2)


def apply_capture(profile, item, sample, metrics, snapshot):
    if metrics['sample_start_seconds'] != 0:
        return {}
    observed = Cut(episode_key(item), metrics['playback_duration'], opening_anchors(sample.words))
    metrics['usable_anchors'] = len(observed.anchors)
    metrics['runtime_compatible_cuts'] = sum(
        cut.episode == observed.episode
        and abs(cut.duration - observed.duration) <= RUNTIME_TOLERANCE
        for cut in snapshot[0].values()
    )
    metrics['most_matching_anchors'] = max(
        (len(set(observed.anchors).intersection(cut.anchors))
         for cut in snapshot[0].values() if cut.episode == observed.episode), default=0
    )
    if len(snapshot) == 2:
        cuts, markers = snapshot
        return write_markers(profile, sample.file_id, observed,
                             markers.get(match_cut(observed, cuts), {}))
    return write_playback_match(profile, sample.file_id, observed, *snapshot)
