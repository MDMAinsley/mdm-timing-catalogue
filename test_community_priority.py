import unittest

from catalogue import Cut, Proposal, Review, select_timing
from export_catalogue import compile_snapshot
from review_queue import build_queue
from vote_summary import AnonymousReview


class CommunityPriorityTests(unittest.TestCase):
    def setUp(self):
        self.signed=Proposal('signed-result',1,'intro',94,126,True)
        self.guest=Proposal('guest-result',1,'intro',124,156,True)
        self.keys={f'{number:064x}' for number in range(1,31)}
        self.guest_votes=[AnonymousReview(key,'guest-result',1,True) for key in self.keys]

    def test_qualified_signed_result_wins_over_larger_anonymous_group(self):
        signed=[Review(account,'signed-result',1,True) for account in range(1,6)]
        result=select_timing([self.signed,self.guest],signed,set(),'intro',1313,
                            anonymous_reviews=self.guest_votes,eligible_keys=self.keys)
        self.assertEqual(result[0],self.signed)
        self.assertEqual(result[1],'community reviewed')

    def test_anonymous_needs_eligible_keys_and_higher_quorum(self):
        result=select_timing([self.guest],[],set(),'intro',1313,anonymous_reviews=self.guest_votes)
        self.assertEqual(result[1],'software generated')
        result=select_timing([self.guest],[],set(),'intro',1313,
                            anonymous_reviews=self.guest_votes,eligible_keys=self.keys)
        self.assertEqual(result[1],'community reviewed')
        short_votes=self.guest_votes[:19]
        result=select_timing([self.guest],[],set(),'intro',1313,
                            anonymous_reviews=short_votes,eligible_keys=self.keys)
        self.assertEqual(result[1],'software generated')

    def test_bad_signed_quorum_cannot_be_overruled_by_anonymous_votes(self):
        signed=[Review(account,'guest-result',1,False) for account in range(1,6)]
        self.assertIsNone(select_timing([self.guest],signed,set(),'intro',1313,
            anonymous_reviews=self.guest_votes,eligible_keys=self.keys))

    def test_review_queue_counts_eligible_but_shows_both_groups(self):
        cut=Cut((123,1,1),1313,(('v1',0,'a'),('v1',16,'b')))
        bad=[AnonymousReview(key,'guest-result',1,False) for key in self.keys]
        empty=build_queue({'cut':cut},{'cut':[self.guest]},[],set(),anonymous_reviews=bad)
        self.assertFalse(empty['entries'])
        flagged=build_queue({'cut':cut},{'cut':[self.guest]},[],set(),
                            anonymous_reviews=bad,eligible_keys=self.keys)
        row=next(iter(flagged['entries'].values()))
        self.assertEqual(row['anonymous']['bad'],30)
        self.assertEqual(row['signed_in']['bad'],0)
        self.assertEqual(row['bad'],30)

    def test_public_export_omits_anonymous_identifiers(self):
        cut=Cut((123,1,1),1313,(('v1',0,'a'),('v1',16,'b')))
        compiled=compile_snapshot({'cut':cut},{'cut':[self.guest]},[],set(),
            anonymous_reviews=self.guest_votes,eligible_keys=self.keys)
        self.assertEqual(next(iter(compiled['cuts'].values()))['markers']['intro']['tier'],'community reviewed')
        for key in self.keys:
            self.assertNotIn(key,str(compiled))
