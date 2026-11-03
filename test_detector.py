import hashlib
import random
import unittest
from unittest.mock import patch

from detector import Invalid, Sample, STEP, SUPPORT, analyse, pair
import worker


def words(seed, n):
    r = random.Random(seed)
    return [r.getrandbits(32) for _ in range(n)]


def sample(episode, data):
    return Sample(episode, hashlib.sha256(str(episode).encode()).hexdigest(), tuple(data), len(data)*STEP+SUPPORT)


def corpus(offsets=(50, 180, 300), second=False):
    theme = words(100, 180)
    rows = []
    for episode, offset in enumerate(offsets, 1):
        data = words(episode, 1100)
        data[offset:offset+180] = theme
        if second:
            data[750:930] = words(200, 180)
        rows.append(sample(episode, data))
    return rows


class DetectionTests(unittest.TestCase):
    def test_cold_open_offsets_and_consensus(self):
        result = analyse(corpus())
        self.assertFalse(result['automatic_skip_eligible'])
        for row, offset in zip(result['results'], (50, 180, 300)):
            self.assertEqual(row['status'], 'provisional_repeat')
            self.assertLess(abs(row['candidate']['start']-offset*STEP), 1)
            self.assertEqual(len(row['candidate']['supporting_episodes']), 2)

    def test_no_theme(self):
        r = analyse([sample(i, words(i, 1000)) for i in (1, 2, 3)])
        self.assertEqual({s['status'] for s in r['results']}, {'no_repeat'})

    def test_silence(self):
        self.assertEqual(pair(sample(1, [0]*1000), sample(2, [0]*1000)), [])

    def test_stationary_tone(self):
        self.assertEqual(pair(sample(1, [35, 72]*500), sample(2, [35, 72]*500)), [])

    def test_two_supported_repeats_ambiguous(self):
        self.assertEqual({r['status'] for r in analyse(corpus(second=True))['results']}, {'ambiguous_repeats'})

    def test_changed_theme_no_two_peer_support(self):
        rows = corpus()
        rows[2] = sample(3, words(9, 1100))
        self.assertEqual({r['status'] for r in analyse(rows)['results']}, {'no_repeat'})

    def test_small_codec_bit_changes(self):
        rows = corpus()
        rows[2] = sample(3, [w ^ (1 << (i % 32)) for i, w in enumerate(rows[2].words)])
        self.assertEqual({r['status'] for r in analyse(rows)['results']}, {'provisional_repeat'})

    def test_short_logo_not_intro(self):
        data = corpus()
        short = words(200, 40)
        data = [sample(i, words(i, 100)+short+words(i+100, 700)) for i in (1, 2, 3)]
        self.assertEqual({r['status'] for r in analyse(data)['results']}, {'no_repeat'})

    def test_duplicate_episode(self):
        rows = corpus()
        with self.assertRaisesRegex(Invalid, 'duplicate_episode'):
            analyse([rows[0], rows[0], rows[2]])

    def test_duplicate_audio(self):
        with self.assertRaisesRegex(Invalid, 'duplicate_audio'):
            analyse([sample(i, words(1, 1000)) for i in (1, 2, 3)])

    def test_need_three(self):
        with self.assertRaises(Invalid):
            analyse(corpus()[:2])

    def test_cancellation(self):
        with self.assertRaisesRegex(Invalid, 'cancelled'):
            analyse(corpus(), lambda: True)

    def test_bad_words(self):
        with self.assertRaises(Invalid):
            sample(1, [1, -1])

    def test_nan_duration(self):
        with self.assertRaises(Invalid):
            Sample(1, 'a'*64, (1,), float('nan'))

    def test_fingerprint_size_limit(self):
        with self.assertRaises(Invalid):
            sample(1, [1]*4001)

    def test_short_duration(self):
        with self.assertRaises(Invalid):
            Sample(1, 'a'*64, tuple(words(1, 1000)), 3)

    def test_invalid_identity(self):
        with self.assertRaises(Invalid):
            Sample(1, 'signed-url', (1,), 10)

    def test_no_paths_in_results(self):
        import json
        result = json.dumps(analyse(corpus()))
        for text in ('path', 'http', 'token', '.wav'):
            self.assertNotIn(text, result)

    def test_urls_rejected_before_io(self):
        job = {'schema': 1, 'show_tmdb': 123, 'season': 1,
               'episodes': [{'episode': i, 'path': 'https://example.invalid/signed'} for i in (1,2,3)]}
        with self.assertRaisesRegex(Invalid, 'local_regular'):
            worker.validate(job)

    def test_schema_validation(self):
        with self.assertRaises(Invalid):
            worker.validate([])

    def test_boolean_schema_rejected(self):
        with self.assertRaises(Invalid):
            worker.validate({'schema': True})

    def test_repeated_entire_content_is_not_intro(self):
        theme = words(200, 1000)
        rows = [sample(i, [w ^ (1 << i) for w in theme]) for i in (1, 2, 3)]
        self.assertEqual({r['status'] for r in analyse(rows)['results']}, {'no_repeat'})

    def test_fourth_episode_supported(self):
        rows = corpus((50, 180, 300, 400))
        result = analyse(rows)
        self.assertEqual({r['status'] for r in result['results']}, {'provisional_repeat'})
        self.assertEqual(len(result['results'][0]['candidate']['supporting_episodes']), 3)

    def test_intro_at_zero(self):
        result = analyse(corpus((0, 50, 180)))
        self.assertEqual({r['status'] for r in result['results']}, {'provisional_repeat'})
        self.assertLess(result['results'][0]['candidate']['start'], 1)

    def test_process_timeout_sanitised(self):
        import subprocess
        with patch('worker.subprocess.run', side_effect=subprocess.TimeoutExpired('PRIVATE_PATH', 1)):
            with self.assertRaisesRegex(Invalid, '^analysis_timeout$'):
                worker.run(['unused'], 1)

    def test_child_failure_sanitised(self):
        from types import SimpleNamespace
        with patch('worker.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(Invalid, '^media_decode_or_fingerprint_failed$'):
                worker.run(['unused'], 1)


if __name__ == '__main__':
    unittest.main()
