import unittest

from catalogue import Cut, Proposal, Review
from review_queue import build_queue


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.cuts = {'cut': Cut((123, 4, 6), 1300, (('v1', 0, 'a'), ('v1', 16, 'b')))}
        self.proposals = {'cut': [Proposal('record', 1, 'intro', 90, 120, True)]}

    def queue(self, votes, proposals=None):
        return build_queue(self.cuts, proposals or self.proposals, votes, set())['entries']

    def test_ten_independent_bad_votes_create_one_task(self):
        votes = [Review(account, 'record', 1, False) for account in range(1, 11)]
        self.assertFalse(self.queue(votes[:9]))
        self.assertEqual(len(self.queue(votes)), 1)
        self.assertEqual(self.queue(votes), self.queue(votes + votes))
        self.assertEqual(next(iter(self.queue(votes).values()))['bad'], 10)

    def test_changed_vote_and_new_revision_do_not_keep_stale_flags(self):
        votes = [Review(account, 'record', 1, False) for account in range(1, 11)]
        self.assertFalse(self.queue(votes + [Review(1, 'record', 1, True)]))
        revised = {'cut': self.proposals['cut'] + [Proposal('record', 2, 'intro', 92, 122, True)]}
        self.assertFalse(self.queue(votes, revised))
        revoked = {'cut': [Proposal('record', 1, 'intro', 90, 120, True, True)]}
        self.assertFalse(self.queue(votes, revoked))

    def test_intro_and_outro_have_independent_thresholds(self):
        proposals = {'cut': self.proposals['cut'] + [Proposal('outro', 1, 'outro', 1250, 1300, True)]}
        votes = [Review(account, 'record', 1, False) for account in range(1, 11)]
        votes += [Review(account, 'outro', 1, False) for account in range(1, 10)]
        entries = self.queue(votes, proposals)
        self.assertEqual([row['marker'] for row in entries.values()], ['intro'])

    def test_a_record_cannot_be_reused_across_cuts(self):
        cuts = dict(self.cuts, other=Cut((123,4,6),1400,()))
        proposals = dict(self.proposals, other=self.proposals['cut'])
        with self.assertRaises(ValueError):
            build_queue(cuts, proposals, [], set())
