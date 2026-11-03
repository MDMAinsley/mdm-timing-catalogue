"""Select shared timings from a validated, maintainer-built catalogue."""

import math
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

RUNTIME_TOLERANCE = 3
from statistics import median


@dataclass(frozen=True)
class Cut:
    episode: tuple
    duration: float
    anchors: tuple


@dataclass(frozen=True)
class Proposal:
    record: str
    revision: int
    marker: str
    start: float
    end: float
    software: bool = False
    withdrawn: bool = False


@dataclass(frozen=True)
class Review:
    account: int
    record: str
    revision: int
    good: bool


def match_cut(observed, candidates):
    """Require two matching position-bound digests under the same method."""
    if not math.isfinite(observed.duration) or observed.duration <= 0:
        return None
    matches = []
    observed_anchors = set(observed.anchors)
    for cut_id, cut in candidates.items():
        if cut.episode != observed.episode or not math.isfinite(cut.duration):
            continue
        if abs(cut.duration - observed.duration) > RUNTIME_TOLERANCE:
            continue
        shared = observed_anchors.intersection(cut.anchors)
        positions = {position for method, position, digest in shared if method and digest}
        if len(positions) >= 2 and max(positions) - min(positions) >= 15:
            matches.append(cut_id)
    return matches[0] if len(matches) == 1 else None


def corrected_range(corrections, duration, minimum_votes=5):
    """Return the median boundaries of a strict majority correction cluster."""
    latest = {
        account: (start, end) for account, start, end in corrections
        if type(account) is int and account > 0 and 0 <= start < end <= duration
    }
    intervals = list(latest.values())
    clusters = set()
    for first_start, _ in intervals:
        for _, first_end in intervals:
            cluster = tuple(
                index for index, (start, end) in enumerate(intervals)
                if first_start <= start <= first_start + 3
                and first_end <= end <= first_end + 3
            )
            if len(cluster) >= minimum_votes and 2 * len(cluster) > len(intervals):
                clusters.add(cluster)
    if not clusters:
        return None
    size = max(map(len, clusters))
    winners = [cluster for cluster in clusters if len(cluster) == size]
    if len(winners) != 1:
        return None
    return tuple(median(intervals[index][boundary] for index in winners[0]) for boundary in (0, 1))


def select_timing(proposals, reviews, trusted_accounts, marker, duration, minimum_votes=5):
    """Return (proposal, provenance) for one already matched cut and marker.

    Reviews arrive in authenticated event order. A newer vote replaces the
    account's previous vote on the same revision.
    """
    if minimum_votes < 1 or not math.isfinite(duration) or duration <= 0:
        return None
    latest = {}
    for proposal in proposals:
        if proposal.marker != marker:
            continue
        previous = latest.get(proposal.record)
        if previous is None or proposal.revision > previous.revision:
            latest[proposal.record] = proposal
        elif proposal.revision == previous.revision and proposal != previous:
            return None

    ballots = {}
    for review in reviews:
        if type(review.account) is int and review.account > 0 and type(review.good) is bool:
            ballots.setdefault((review.record, review.revision), {})[review.account] = review.good

    eligible = []
    for proposal in latest.values():
        if proposal.withdrawn or not 0 <= proposal.start < proposal.end <= duration:
            continue
        votes = ballots.get((proposal.record, proposal.revision), {})
        trusted_votes = {votes[account] for account in trusted_accounts if account in votes}
        if False in trusted_votes:
            continue
        good = sum(votes.values())
        if True in trusted_votes:
            eligible.append((3, proposal, "trusted reviewer"))
        elif len(votes) >= minimum_votes and 4 * good > 3 * len(votes):
            eligible.append((2, proposal, "community reviewed"))
        elif proposal.software and not (len(votes) >= minimum_votes and 2 * good <= len(votes)):
            eligible.append((1, proposal, "software generated"))

    if not eligible:
        return None
    highest = max(entry[0] for entry in eligible)
    best = [entry for entry in eligible if entry[0] == highest]
    if len({(entry[1].start, entry[1].end) for entry in best}) != 1:
        return None
    _, proposal, provenance = best[0]
    return proposal, provenance


def lookup(observed, cuts, proposals_by_cut, reviews, trusted_accounts, marker):
    """Keep review selection scoped to the uniquely matched cut."""
    cut_id = match_cut(observed, cuts)
    if cut_id is None:
        return None
    return select_timing(
        proposals_by_cut.get(cut_id, ()), reviews, trusted_accounts, marker, observed.duration
    )


def write_playback_match(profile, file_id, observed, cuts, proposals_by_cut, reviews, trusted_accounts):
    """Atomically replace the local result, including on a lookup miss."""
    markers = {}
    for kind in ('intro', 'outro'):
        result = lookup(observed, cuts, proposals_by_cut, reviews, trusted_accounts, kind)
        if result:
            proposal, tier = result
            markers[kind] = dict(start=proposal.start, end=proposal.end, tier=tier)
    return write_markers(profile, file_id, observed, markers)


def write_markers(profile, file_id, observed, markers):
    tmdb_id, season, episode = observed.episode
    destination = Path(profile) / 'shared_timing_match.json'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=destination.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(dict(schema=1, file_id=file_id, tmdb_id=tmdb_id, season=season,
                           episode=episode, duration=observed.duration, markers=markers), stream)
        temporary.replace(destination)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return markers
