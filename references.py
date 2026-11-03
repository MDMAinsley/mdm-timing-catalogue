"""Compact provisional references; independent episode support, never verified intros."""
import hashlib
import json
import math

from detector import Invalid, Sample, STEP, SUPPORT, analyse, pair_evidence

METHOD = 'chromaprint-2-reference-v2'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')


def check_identity(show, season):
    if type(show) is not int or not 1 <= show <= 10000000 or type(season) is not int or not 0 <= season <= 10000:
        raise Invalid('invalid_show_or_season')


def learn(show, season, samples, cancelled=lambda: False):
    check_identity(show, season)
    report = analyse(samples, cancelled)
    eligible = [r for r in report['results'] if r['status'] == 'provisional_repeat']
    if len(eligible) < 3:
        raise Invalid('no_unambiguous_reference')
    clips = []
    for result in eligible[:3]:
        sample = next(s for s in samples if s.episode == result['episode'])
        interval = result['candidate']
        lo = max(0, math.floor((interval['start']-SUPPORT)/STEP))
        hi = min(len(sample.words), math.ceil((interval['end']+SUPPORT)/STEP))
        clips.append({'episode': sample.episode, 'file_id': sample.file_id,
                      'words': list(sample.words[lo:hi]),
                      'seconds': (hi-lo)*STEP+SUPPORT})
    body = {'schema': 1, 'method': METHOD, 'show_tmdb': show, 'season': season,
            'automatic_skip_eligible': False, 'clips': clips}
    body['reference_id'] = hashlib.sha256(encoded(body)).hexdigest()
    return validate(body)


def validate(ref):
    if not isinstance(ref, dict) or type(ref.get('schema')) is not int or ref['schema'] != 1 or ref.get('method') != METHOD:
        raise Invalid('incompatible_reference')
    check_identity(ref.get('show_tmdb'), ref.get('season'))
    if ref.get('automatic_skip_eligible') is not False or set(ref) != {
            'schema', 'method', 'show_tmdb', 'season', 'automatic_skip_eligible', 'clips', 'reference_id'}:
        raise Invalid('invalid_reference')
    clips = ref.get('clips')
    if not isinstance(clips, list) or not 2 <= len(clips) <= 3:
        raise Invalid('invalid_reference_clips')
    samples = []
    for clip in clips:
        if not isinstance(clip, dict) or set(clip) != {'episode', 'file_id', 'words', 'seconds'}:
            raise Invalid('invalid_reference_clip')
        if not isinstance(clip['words'], list) or not 100 <= len(clip['words']) <= 1100:
            raise Invalid('invalid_reference_length')
        samples.append(Sample(clip['episode'], clip['file_id'], tuple(clip['words']), clip['seconds']))
    if len({s.episode for s in samples}) != len(samples) or len({s.file_id for s in samples}) != len(samples):
        raise Invalid('duplicate_reference_support')
    body = dict(ref)
    supplied = body.pop('reference_id')
    if supplied != hashlib.sha256(encoded(body)).hexdigest():
        raise Invalid('reference_digest_mismatch')
    return ref


def match(ref, show, season, target, cancelled=lambda: False, *, cross_season=False):
    validate(ref)
    check_identity(show, season)
    same_season = season == ref['season']
    if show != ref['show_tmdb'] or (not same_season and not cross_season):
        raise Invalid('reference_identity_mismatch')
    supports = []
    for clip in ref['clips']:
        # A training episode cannot vote for itself, even if re-encoded.
        if (same_season and clip['episode'] == target.episode) or clip['file_id'] == target.file_id:
            continue
        source = Sample(clip['episode'], clip['file_id'], tuple(clip['words']), clip['seconds'])
        for row in pair_evidence(target, source, cancelled, template=True):
            supports.append((source.episode, row['a_start'], row['a_end'], row['mean_bits']))
    groups = []
    for seed in supports:
        rows = [r for r in supports if abs(r[1]-seed[1]) <= 3 and abs(r[2]-seed[2]) <= 3]
        peers = {r[0] for r in rows}
        start, end = max(r[1] for r in rows), min(r[2] for r in rows)
        if len(peers) >= 2 and end-start >= 12 and not any(abs(start-g['start']) < 3 for g in groups):
            groups.append({'start': round(start, 3), 'end': round(end, 3),
                           'supporting_episodes': sorted(peers),
                           'mean_bits': round(sum(r[3] for r in rows)/len(rows), 3)})
    state = 'no_repeat' if not groups else 'provisional_repeat' if len(groups) == 1 else 'ambiguous_repeats'
    return {'schema': 1, 'method': METHOD, 'show_tmdb': show, 'season': season,
            'episode': target.episode, 'file_id': target.file_id, 'reference_id': ref['reference_id'],
            'status': state, 'candidate': groups[0] if len(groups) == 1 else None,
            'automatic_skip_eligible': False,
            'note': 'Reference audio match is provisional; semantic identity and boundaries remain unvalidated.'}
