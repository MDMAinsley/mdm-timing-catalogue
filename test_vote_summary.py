import unittest

from catalogue import Review
from vote_summary import AnonymousReview, summary


class VoteSummaryTests(unittest.TestCase):
    def test_separate_totals_and_percentages(self):
        signed = [Review(1,'intro',1,True),Review(2,'intro',1,False)]
        guest = [AnonymousReview('a'*64,'intro',1,True),AnonymousReview('b'*64,'intro',1,True)]
        result = summary(signed,guest,'intro',1)
        self.assertEqual(result['signed_in']['positive_percent'],50)
        self.assertEqual(result['anonymous']['positive_percent'],100)
        self.assertEqual(result['signed_in']['total'],2)
        self.assertEqual(result['anonymous']['total'],2)
        self.assertNotIn('a'*64,str(result))

    def test_changed_vote_replaces_repeat_and_other_revision_is_excluded(self):
        votes=[AnonymousReview('a'*64,'intro',1,True),
               AnonymousReview('a'*64,'intro',1,False),
               AnonymousReview('a'*64,'intro',2,True)]
        result=summary([],votes,'intro',1)['anonymous']
        self.assertEqual(result,dict(good=0,bad=1,total=1,positive_percent=0))

    def test_linked_guest_key_is_not_counted_again_as_anonymous(self):
        signed=[Review(123,'intro',1,True)]
        guests=[AnonymousReview('a'*64,'intro',1,True)]
        result=summary(signed,guests,'intro',1,linked_keys=['a'*64])
        self.assertEqual(result['signed_in']['total'],1)
        self.assertEqual(result['anonymous']['total'],0)

    def test_anonymous_identifier_cannot_be_a_signed_in_identity(self):
        signed=[Review('a'*64,'intro',1,True)]
        result=summary(signed,[],'intro',1)
        self.assertEqual(result['signed_in']['total'],0)
