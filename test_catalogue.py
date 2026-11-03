import unittest

from catalogue import Cut, Proposal, Review, corrected_range, match_cut, select_timing


class CatalogueTests(unittest.TestCase):
    def test_same_episode_two_cuts_and_ambiguous_evidence(self):
        episode = (123, 4, 15)
        common = ("v1", 30, "common")
        short = Cut(episode, 1313, (common, ("v1", 60, "short")))
        long = Cut(episode, 1314, (common, ("v1", 60, "long")))
        self.assertEqual(match_cut(short, {"short": short, "long": long}), "short")
        self.assertEqual(match_cut(Cut(episode, 1315.48, short.anchors), {"short": short}), "short")
        self.assertIsNone(match_cut(Cut(episode, 1316.01, short.anchors), {"short": short}))
        self.assertEqual(match_cut(long, {"short": short, "long": long}), "long")
        self.assertIsNone(match_cut(short, {"short": short, "alias": short}))
        self.assertIsNone(match_cut(Cut(episode, 1313, (common,)), {"short": short}))

    def test_trust_and_revision_boundaries(self):
        software = Proposal("software", 1, "intro", 94, 126, software=True)
        corrected = Proposal("corrected", 2, "intro", 96, 127)
        proposals = [software, corrected]
        votes = [Review(account, "corrected", 2, account <= 6) for account in range(1, 9)]
        self.assertEqual(select_timing(proposals, votes, set(), "intro", 1300)[0], software)
        votes.append(Review(8, "corrected", 2, True))
        self.assertEqual(select_timing(proposals, votes, set(), "intro", 1300)[1], "community reviewed")
        votes.append(Review(9, "software", 1, True))
        self.assertEqual(select_timing(proposals, votes, {9}, "intro", 1300)[1], "trusted reviewer")
        proposals.append(Proposal("software", 2, "intro", 94, 126, withdrawn=True))
        self.assertEqual(select_timing(proposals, votes, {9}, "intro", 1300)[0], corrected)
        self.assertIsNone(select_timing(proposals, votes, {9}, "outro", 1300))

    def test_correction_cluster_requires_independent_agreement_at_both_ends(self):
        corrections = [(1, 96, 126), (2, 96, 128), (3, 96, 127), (4, 97, 127),
                       (5, 95, 127), (6, 47, 77), (7, 96, 150)]
        self.assertEqual(corrected_range(corrections, 1300), (96, 127))
        self.assertIsNone(corrected_range([(1, 96, 127)] * 10, 1300))
        self.assertIsNone(corrected_range(corrections + [(8, 47, 77), (9, 47, 77),
                                                        (10, 47, 77)], 1300))
