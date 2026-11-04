"""Build one manual review entry per current marker revision at ten Bad votes."""
import argparse
import hashlib
import json
from pathlib import Path

from live_catalogue import load_snapshot


def build_queue(cuts, proposals, reviews, trusted_accounts, minimum_bad=10):
    if type(minimum_bad) is not int or minimum_bad < 1:
        raise ValueError('invalid_review_threshold')
    ballots = {}
    for vote in reviews:
        if type(vote.account) is int and vote.account > 0 and type(vote.good) is bool:
            ballots.setdefault((vote.record, vote.revision), {})[vote.account] = vote.good
    queue = {}
    owners = {}
    for cut_key, cut in cuts.items():
        latest = {}
        for proposal in proposals[cut_key]:
            owner = (cut_key, proposal.marker)
            if proposal.record in owners and owners[proposal.record] != owner:
                raise ValueError('record_identity_reused')
            owners[proposal.record] = owner
            previous = latest.get(proposal.record)
            if previous is None or proposal.revision > previous.revision:
                latest[proposal.record] = proposal
            elif previous.revision == proposal.revision and previous != proposal:
                raise ValueError('conflicting_record_revision')
        for proposal in latest.values():
            if proposal.withdrawn:
                continue
            votes = ballots.get((proposal.record, proposal.revision), {})
            bad = sum(not good for good in votes.values())
            if bad < minimum_bad:
                continue
            identity = [cut.episode, cut.duration, sorted(set(cut.anchors)),
                        proposal.marker, proposal.record, proposal.revision]
            key = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
            queue[key] = dict(
                episode=cut.episode, duration=cut.duration, marker=proposal.marker,
                revision=proposal.revision, start=proposal.start, end=proposal.end,
                good=sum(votes.values()), bad=bad, reason='ten_bad_ratings',
            )
    return dict(schema=1, entries=queue)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    ledger = load_snapshot(args.ledger)
    if len(ledger) != 4:
        parser.error('Input must be a private schema 1 review ledger')
    result = build_queue(*ledger)
    with args.destination.open('x', encoding='utf8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
