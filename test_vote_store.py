from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from vote_store import Votes


class VoteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Votes(Path(self.temp.name)/'votes.sqlite')
        self.guest=self.store.anonymous('a'*64)
        self.account=self.store.signed_in(123)
        self.first=self.store.target('b'*64,'intro','first-cut-intro',1,94,126,1313)
        self.second=self.store.target('c'*64,'intro','second-cut-intro',1,124,156,1313)

    def test_two_stream_cuts_get_independent_votes_and_same_cut_reuses_target(self):
        self.store.cast(self.guest,self.first,True)
        self.store.cast(self.guest,self.second,False)
        same=self.store.target('b'*64,'intro','first-cut-intro',1,94,126,1313)
        self.assertEqual(same,self.first)
        self.store.cast(self.guest,same,False)
        self.assertEqual(self.store.totals(self.first)['anonymous']['total'],1)
        self.assertEqual(self.store.totals(self.first)['anonymous']['bad'],1)
        self.assertEqual(self.store.totals(self.second)['anonymous']['bad'],1)

    def test_account_survives_multiple_signins_and_intro_outro_are_separate(self):
        self.assertEqual(self.account,self.store.signed_in(123))
        outro=self.store.target('b'*64,'outro','first-cut-outro',1,1250,1313,1313)
        self.store.cast(self.account,self.first,True)
        self.store.cast(self.account,outro,False)
        self.assertEqual(self.store.totals(self.first)['signed_in']['good'],1)
        self.assertEqual(self.store.totals(outro)['signed_in']['bad'],1)

    def test_link_migrates_guest_without_double_counting_and_disables_guest_credential(self):
        self.store.cast(self.guest,self.first,True)
        self.store.link(self.guest,self.account)
        self.store.link(self.guest,self.account)
        counts=self.store.totals(self.first)
        self.assertEqual(counts['anonymous']['total'],0)
        self.assertEqual(counts['signed_in']['total'],1)
        with self.assertRaises(ValueError):
            self.store.cast(self.guest,self.first,False)
        other=self.store.signed_in(456)
        with self.assertRaises(ValueError):
            self.store.link(self.guest,other)

    def test_existing_signed_in_choice_wins_over_guest_choice(self):
        self.store.cast(self.account,self.first,False)
        self.store.cast(self.guest,self.first,True)
        self.store.link(self.guest,self.account)
        self.assertEqual(self.store.totals(self.first)['signed_in']['bad'],1)

    def test_revisions_cannot_change_the_cut_or_range_and_new_revision_has_fresh_votes(self):
        self.store.cast(self.guest,self.first,True)
        with self.assertRaises(ValueError):
            self.store.target('c'*64,'intro','first-cut-intro',2,124,156,1313)
        with self.assertRaises(ValueError):
            self.store.target('b'*64,'intro','first-cut-intro',1,95,127,1313)
        newer=self.store.target('b'*64,'intro','first-cut-intro',2,95,127,1313)
        self.assertEqual(self.store.totals(newer)['anonymous']['total'],0)

    def test_concurrent_retry_of_same_vote_counts_once(self):
        def retry(_):
            self.store.cast(self.guest,self.first,True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(retry,range(12)))
        self.assertEqual(self.store.totals(self.first)['anonymous']['total'],1)
        with self.store.db() as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
