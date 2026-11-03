"""Original experimental repeated-audio matcher. No semantic intro claim or Kodi calls."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import math

# Chromaprint algorithm 2: frame 4096, hop 1365, rate 11025.
STEP = 1365 / 11025
# Support width: (5 chroma-filter frames + 16 classifier frames - 2) hops + overlap.
SUPPORT = (19 * 1365 + 2731) / 11025
MIN_SECONDS = 12
MAX_SECONDS = 120
MAX_WORDS = 4000


class Invalid(ValueError):
    pass


@dataclass(frozen=True)
class Sample:
    episode: int
    file_id: str
    words: tuple
    seconds: float

    def __post_init__(self):
        if type(self.episode) is not int or not 1 <= self.episode <= 10000:
            raise Invalid('invalid_episode')
        if not isinstance(self.file_id, str) or len(self.file_id) != 64 or any(c not in '0123456789abcdef' for c in self.file_id):
            raise Invalid('invalid_file_identity')
        if not isinstance(self.words, tuple) or not 1 <= len(self.words) <= MAX_WORDS:
            raise Invalid('invalid_fingerprint_length')
        if any(type(v) is not int or not 0 <= v <= 0xffffffff for v in self.words):
            raise Invalid('invalid_fingerprint_word')
        if type(self.seconds) not in (int, float) or not math.isfinite(self.seconds) or not 1 <= self.seconds <= 480:
            raise Invalid('invalid_analysis_duration')
        if (len(self.words)-1) * STEP + SUPPORT > self.seconds + .5:
            raise Invalid('fingerprint_exceeds_audio')


def informative(words):
    """Avoid constant/silent fingerprints and very repetitive stationary tones."""
    return len(words) >= 25 and len(set(words)) >= max(12, len(words)//10)


def pair(a, b, cancelled=lambda: False, template=False, minimum_seconds=None):
    """Return matching interiors on promising offset diagonals, not exact boundaries."""
    if not informative(a.words) or not informative(b.words):
        return []
    index = defaultdict(list)
    for j, word in enumerate(b.words):
        for shift in (0, 8, 16, 24):
            index[shift, (word >> shift) & 255].append(j)
    votes = Counter()
    for i in range(0, len(a.words), 4):
        if cancelled():
            raise Invalid('cancelled')
        for shift in (0, 8, 16, 24):
            hits = index[shift, (a.words[i] >> shift) & 255]
            # Large buckets contribute mostly noise; bound work on low entropy audio.
            if len(hits) <= 80:
                for j in hits:
                    if (a.words[i] ^ b.words[j]).bit_count() <= 6:
                        votes[j-i] += 1
    offsets = []
    for delta, count in votes.most_common():
        if count < 10:
            break
        if not any(abs(delta-d) <= 2 for d in offsets):
            offsets.append(delta)
            if len(offsets) == 12:
                break
    found = []
    if minimum_seconds not in (None, 5):
        raise Invalid('unsupported_anchor_minimum')
    minimum = math.ceil((MIN_SECONDS if minimum_seconds is None else minimum_seconds) / STEP)
    for delta in offsets:
        if cancelled():
            raise Invalid('cancelled')
        lo, hi = max(0, -delta), min(len(a.words), len(b.words)-delta)
        distances = [(a.words[i] ^ b.words[i+delta]).bit_count() for i in range(lo, hi)]
        # Smooth over about one second; this is tolerant to codecs, not time stretching.
        good = [False] * len(distances)
        for k in range(4, len(distances)-4):
            good[k] = sum(distances[k-4:k+5]) / 9 <= 4
        begin = None
        for k in range(len(good)+1):
            if k < len(good) and good[k]:
                if begin is None:
                    begin = k
                continue
            if begin is not None:
                count = k-begin
                start = lo+begin
                if count >= minimum:
                    seconds = count * STEP
                    if seconds <= MAX_SECONDS and (template or seconds <= min(a.seconds, b.seconds)*.75):
                        section = a.words[start:start+count]
                        if informative(section):
                            found.append({'a_start': start*STEP, 'a_end': (start+count)*STEP,
                                          'b_start': (start+delta)*STEP, 'b_end': (start+delta+count)*STEP,
                                          'mean_bits': sum(distances[begin:k])/count})
                begin = None
    # Adjacent offset estimates describe the same span; retain the best one.
    unique = []
    for row in sorted(found, key=lambda r: (r['mean_bits'], -(r['a_end']-r['a_start']))):
        if not any(abs(row['a_start']-r['a_start']) < 3 and abs(row['b_start']-r['b_start']) < 3 for r in unique):
            unique.append(row)
    return unique


def pair_evidence(a, b, cancelled=lambda: False, template=False):
    """Continuous matches, plus conservative two-anchor edit hypotheses.

    The unmatched middle is NEVER verified content. These are provisional spans
    for reference learning/diagnostics, not permission to seek across that gap.
    """
    continuous = pair(a, b, cancelled, template)
    result = [dict(row, kind='continuous', matched_seconds=row['a_end']-row['a_start'])
              for row in continuous]
    fragments = pair(a, b, cancelled, template, minimum_seconds=5)
    fragments = sorted((r for r in fragments if r['mean_bits'] <= 2.5),
                       key=lambda r: (r['mean_bits'], -(r['a_end']-r['a_start'])))[:24]
    for left in fragments:
        if cancelled():
            raise Invalid('cancelled')
        for right in fragments:
            agap, bgap = right['a_start']-left['a_end'], right['b_start']-left['b_end']
            # Ordered, disjoint anchors; one rendition must be nearly continuous.
            if not (0 <= agap <= 18 and 0 <= bgap <= 18 and min(agap, bgap) <= 3 and abs(agap-bgap) >= 2):
                continue
            matched = (left['a_end']-left['a_start'])+(right['a_end']-right['a_start'])
            aspan, bspan = right['a_end']-left['a_start'], right['b_end']-left['b_start']
            if matched < MIN_SECONDS or max(aspan, bspan) > MAX_SECONDS:
                continue
            if matched/max(aspan, bspan) < .45:
                continue
            if not template and (aspan > a.seconds*.9 or bspan > b.seconds*.9 or matched > min(a.seconds, b.seconds)*.75):
                continue
            row = {'a_start': left['a_start'], 'a_end': right['a_end'],
                   'b_start': left['b_start'], 'b_end': right['b_end'],
                   'mean_bits': (left['mean_bits']+right['mean_bits'])/2,
                   'kind': 'edited_two_anchor', 'matched_seconds': matched,
                   'unverified_middle_seconds': {'a': agap, 'b': bgap},
                   'anchors': [dict(left), dict(right)]}
            if not any(abs(row['a_start']-r['a_start']) < 3 and abs(row['a_end']-r['a_end']) < 3 and
                       abs(row['b_start']-r['b_start']) < 3 and abs(row['b_end']-r['b_end']) < 3 for r in result):
                result.append(row)
    return result


def analyse(samples, cancelled=lambda: False):
    if not 3 <= len(samples) <= 4:
        raise Invalid('need_three_or_four_distinct_episodes')
    if len({s.episode for s in samples}) != len(samples) or len({s.file_id for s in samples}) != len(samples):
        raise Invalid('duplicate_episode_or_file')
    fingerprints = {hashlib.sha256(b''.join(w.to_bytes(4, 'little') for w in s.words)).digest() for s in samples}
    if len(fingerprints) != len(samples):
        raise Invalid('duplicate_audio')
    supports = defaultdict(list)
    for i, a in enumerate(samples):
        for b in samples[i+1:]:
            for r in pair_evidence(a, b, cancelled):
                supports[a.episode].append((b.episode, r['a_start'], r['a_end'], r['mean_bits']))
                supports[b.episode].append((a.episode, r['b_start'], r['b_end'], r['mean_bits']))
    results = []
    for s in samples:
        groups = []
        for seed in supports[s.episode]:
            compatible = [r for r in supports[s.episode] if abs(r[1]-seed[1]) <= 3 and abs(r[2]-seed[2]) <= 3]
            peers = {r[0] for r in compatible}
            if len(peers) >= 2:
                start, end = max(r[1] for r in compatible), min(r[2] for r in compatible)
                if end-start >= MIN_SECONDS and not any(abs(start-g['start']) < 3 for g in groups):
                    groups.append({'start': round(start, 3), 'end': round(end, 3),
                                   'supporting_episodes': sorted(peers),
                                   'mean_bits': round(sum(r[3] for r in compatible)/len(compatible), 3)})
        row = {'episode': s.episode, 'file_id': s.file_id, 'status': 'no_repeat', 'candidate': None}
        if len(groups) == 1:
            row.update(status='provisional_repeat', candidate=groups[0])
        elif len(groups) > 1:
            row.update(status='ambiguous_repeats')
        results.append(row)
    return {'schema': 1, 'method': 'chromaprint-2-opening-repeat-v2',
            'automatic_skip_eligible': False, 'boundary_uncertainty_seconds': round(SUPPORT, 3),
            'note': 'Repeated audio is not proof of an intro; boundaries and any edited middle remain unverified.',
            'results': results}
