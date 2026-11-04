"""Keep authenticated and anonymous review totals separate."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AnonymousReview:
    contributor_hash: str
    record: str
    revision: int
    good: bool


def summary(reviews, anonymous_reviews, record, revision, linked_keys=()):
    """Inputs must come from service-verified contributor records.

    linked_keys identifies anonymous keys already associated with a signed-in
    account. Their guest votes are excluded to prevent counting both identities.
    """
    signed_in = {}
    anonymous = {}
    linked_keys = set(linked_keys)
    for review in reviews:
        if (review.record == record and review.revision == revision
                and type(review.account) is int and review.account > 0
                and type(review.good) is bool):
            signed_in[review.account] = review.good
    for review in anonymous_reviews:
        key = review.contributor_hash
        if (review.record == record and review.revision == revision
                and isinstance(key, str) and re.fullmatch('[0-9a-f]{64}', key)
                and key not in linked_keys and type(review.good) is bool):
            anonymous[key] = review.good
    result = {}
    for name, votes in (('signed_in', signed_in), ('anonymous', anonymous)):
        total = len(votes)
        good = sum(votes.values())
        result[name] = dict(good=good, bad=total-good, total=total,
                            positive_percent=round(100*good/total, 2) if total else None)
    return result
