"""Compile a private review ledger into an account-free playback snapshot."""
import argparse
import hashlib
import json
from pathlib import Path

from catalogue import select_timing
from live_catalogue import load_snapshot


def cut_identifier(cut):
    identity = [cut.episode, float(cut.duration), sorted(set(cut.anchors))]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


def marker_identifier(cut_id, kind, start, end):
    return hashlib.sha256(json.dumps([cut_id, kind, float(start), float(end)], separators=(',', ':')).encode()).hexdigest()


def compile_snapshot(cuts, proposals, reviews, trusted_accounts, *, anonymous_reviews=(),
                     eligible_keys=(), linked_keys=(), anonymous_minimum=20):
    reviews=tuple(reviews)
    anonymous_reviews=tuple(anonymous_reviews)
    trusted_accounts=set(trusted_accounts)
    eligible_keys=set(eligible_keys)
    linked_keys=set(linked_keys)
    output = {}
    for key, cut in cuts.items():
        identity = [cut.episode, cut.duration, sorted(set(cut.anchors))]
        cut_id = cut_identifier(cut)
        if cut_id in output:
            raise ValueError('duplicate_cut_identity')
        if len({position for _, position, _ in cut.anchors}) < 2:
            raise ValueError('insufficient_cut_anchors')
        markers = {}
        for kind in ('intro', 'outro'):
            result = select_timing(proposals[key], reviews, trusted_accounts, kind, cut.duration,
                anonymous_reviews=anonymous_reviews,eligible_keys=eligible_keys,
                linked_keys=linked_keys,anonymous_minimum=anonymous_minimum)
            if result:
                proposal, tier = result
                marker_id = marker_identifier(cut_id, kind, proposal.start, proposal.end)
                markers[kind] = dict(id=marker_id, revision=proposal.revision,
                                     start=proposal.start, end=proposal.end, tier=tier)
        output[cut_id] = dict(episode=cut.episode, duration=cut.duration,
                              anchors=identity[2], markers=markers)
    return dict(schema=2, cuts=output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    ledger = load_snapshot(args.ledger)
    if len(ledger) != 4:
        parser.error('The input must be a schema 1 review ledger')
    payload = json.dumps(compile_snapshot(*ledger), sort_keys=True, indent=2, allow_nan=False)
    if len(payload) > 2_000_000:
        parser.error('The snapshot exceeds the playback reader limit')
    with args.destination.open('x', encoding='utf8') as stream:
        stream.write(payload + '\n')


if __name__ == '__main__':
    main()
