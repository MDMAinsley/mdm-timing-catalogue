import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest

from catalogue import Cut, Proposal, Review
from export_catalogue import compile_snapshot
from live_catalogue import apply_capture, load_snapshot, opening_anchors


class ExportTests(unittest.TestCase):
    def test_export_redacts_ledger_and_matches_two_shows(self):
        cuts, proposals, samples = {}, {}, {}
        for show in (2316, 19885):
            rng = random.Random(show)
            words = tuple(rng.getrandbits(32) for _ in range(200))
            samples[show] = SimpleNamespace(words=words, file_id='a' * 64)
            cuts[show] = Cut((show, 1, 1), 1300, opening_anchors(words))
            proposals[show] = [Proposal(f'private-record-{show}', 1, 'intro', 94, 126, True)]
        reviews = [Review(928374, 'private-record-2316', 1, True)]
        compiled = compile_snapshot(cuts, proposals, reviews, {928374})
        encoded = json.dumps(compiled)
        self.assertNotIn('928374', encoded)
        self.assertNotIn('private-record', encoded)
        self.assertNotIn('reviews', compiled)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            path.write_text(encoded, encoding='utf8')
            snapshot = load_snapshot(path)
            for show, tier in ((2316, 'trusted reviewer'), (19885, 'software generated')):
                item = dict(tmdb_id=show, season=1, episode=1)
                metrics = dict(sample_start_seconds=0, playback_duration=1300)
                markers = apply_capture(folder, item, samples[show], metrics, snapshot)
                self.assertEqual(markers['intro']['tier'], tier)
            self.assertEqual(apply_capture(folder, item, samples[2316], metrics, snapshot), {})
