"""Read-only Kodi TCP JSON-RPC and finite live selected-episode audio capture."""
import argparse
import codecs
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import wave

from cache import Cache
from detector import Invalid, Sample
from live_transport import Reader, relay
from references import learn, match
from worker import run


def rpc(method, params=None):
    allowed_bridge = method in ('Addons.GetAddonDetails', 'Addons.ExecuteAddon') and isinstance(params, dict) and params.get('addonid') == 'script.mdm.audio.diagnostic'
    if not allowed_bridge and method not in ('Player.GetActivePlayers', 'Player.GetItem', 'Player.GetProperties', 'Player.GetChapters', 'XBMC.GetInfoBooleans', 'XBMC.GetInfoLabels'):
        raise Invalid('read_only_rpc_only')
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params is not None:
        request['params'] = params
    try:
        with socket.create_connection(('127.0.0.1', 9090), timeout=2) as stream:
            stream.sendall(json.dumps(request).encode())
            decoder, text = json.JSONDecoder(), ''
            utf8 = codecs.getincrementaldecoder('utf8')()
            while len(text) <= 262144:
                part = stream.recv(16384)
                if not part:
                    break
                text += utf8.decode(part)
                while text.lstrip():
                    try:
                        data, length = decoder.raw_decode(text.lstrip())
                    except ValueError:
                        break
                    text = text.lstrip()[length:]
                    if data.get('id') == 1:
                        if 'error' in data:
                            raise Invalid('kodi_read_request_failed')
                        return data['result']
        raise Invalid('kodi_response_unavailable')
    except OSError:
        raise Invalid('kodi_not_reachable') from None


def snapshot(allow_missing_identity=False):
    players = [p for p in rpc('Player.GetActivePlayers') if p.get('type') == 'video']
    if len(players) != 1:
        raise Invalid('start_an_episode_in_kodi')
    player = players[0]['playerid']
    item = rpc('Player.GetItem', {'playerid': player, 'properties': ['file', 'season', 'episode']})['item']
    props = rpc('Player.GetProperties', {'playerid': player, 'properties': ['time', 'totaltime', 'speed']})
    if any(type(item.get(k)) is not int or item[k] < 0 for k in ('season', 'episode')):
        labels = rpc('XBMC.GetInfoLabels', {'labels': ['VideoPlayer.Season', 'VideoPlayer.Episode']})
        try:
            item['season'] = int(labels['VideoPlayer.Season'])
            item['episode'] = int(labels['VideoPlayer.Episode'])
        except (KeyError, ValueError, TypeError):
            if not allow_missing_identity:
                raise Invalid('playing_episode_identity_unavailable') from None
            item['season'] = item['episode'] = None
    address = item.get('file')
    if not isinstance(address, str) or not address:
        raise Invalid('selected_stream_address_unavailable')
    return {'player': player, 'url': address, 'season': item.get('season'), 'episode': item.get('episode'), 'props': props}


def identity(current, profile):
    path = profile/'last_timing_match.json'
    if path.stat().st_size > 65536:
        raise Invalid('timing_report_unavailable')
    report = json.loads(path.read_text(encoding='utf8'))
    item = report.get('episode', {})
    if not isinstance(item, dict):
        raise Invalid('timing_identity_unavailable')
    if (not 0 <= time.time()-report.get('updated_utc', 0) <= 900 or
            report.get('status') not in ('matched', 'partial', 'unverified', 'selected', 'resolved')):
        raise Invalid('fresh_matching_flix_report_required')
    from references import check_identity
    check_identity(item.get('tmdb_id'), item.get('season'))
    if (item.get('season'), item.get('episode')) != (current['season'], current['episode']):
        raise Invalid('playing_episode_does_not_match_report')
    return item


def chapter_window(chapters):
    if not isinstance(chapters, list):
        raise Invalid('no_labelled_intro_window')
    first = last = None
    for chapter in chapters[:200]:
        name = str(chapter.get('name', '')).strip().lower()
        at = chapter.get('time')
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= 86400:
            raise Invalid('invalid_chapter_window')
        intro = name in ('intro', 'opening', 'opening credits', 'title sequence', 'opening titles')
        if first is None and intro:
            first = at
        elif first is not None and not intro:
            last = at
            break
    if first is None or last is None or not 0 < last-first <= 120 or first > 600:
        raise Invalid('no_labelled_intro_window')
    pad = max(7, math.ceil((last-first)*.25))
    offset = max(0, math.floor(first-pad))
    return offset, max(30, math.ceil(last+pad-offset))


def hint_window(start, end):
    """A provider range can guide collection without supplying a seek target."""
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (start, end)):
        raise Invalid('invalid_timing_hint')
    if not 0 <= start <= 600 or not 3 <= end - start <= 120:
        raise Invalid('invalid_timing_hint')
    offset = max(0, math.floor(start) - 15)
    return offset, max(30, math.ceil(end) + 5 - offset)


def provider_window(profile, file_id, item):
    path = profile/'audio_search_hint.json'
    try:
        if time.time() - path.stat().st_mtime > 900:
            return None
        with path.open(encoding='utf8') as stream:
            row = json.loads(stream.read(4097))
        if row.get('schema') != 1 or row.get('file_id') != file_id:
            return None
        if any(row.get(name) != item[name] for name in ('tmdb_id', 'season', 'episode')):
            return None
        return hint_window(row['start'], row['end'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def select_references(caches, item):
    show, season = item['tmdb_id'], item['season']
    found = [reference for cache in caches if (reference := cache.reference(show, season))]
    if not found:
        found = [reference for cache in caches if (reference := cache.nearby_reference(show, season))]
        if found:
            selected_season = min(ref['season'] for ref in found)
            found = [ref for ref in found if ref['season'] == selected_season]
    return list({ref['reference_id']: ref for ref in found}.values())


def capture(ffmpeg, fpcalc, profile, seconds=240, max_bytes=64*1024*1024, use_bridge=False, use_chapter_window=False, window_start=0, blind_cache=None, extra_reference_caches=(), timing_hint=None, use_provider_hints=False, auto_reference=False, expected_file=None, catalogue_episodes=None):
    if type(seconds) is not int or not 30 <= seconds <= 480:
        raise Invalid('invalid_opening_limit')
    if type(window_start) is not int or not 0 <= window_start <= 600:
        raise Invalid('invalid_sample_start')
    hinted = hint_window(*timing_hint) if timing_hint is not None else None
    current = snapshot(allow_missing_identity=use_bridge)
    offset = window_start
    if use_chapter_window:
        marks = rpc('Player.GetChapters', {'playerid': current['player']}).get('chapters', [])
        offset, seconds = chapter_window(marks)
    if use_bridge:
        from kodi_bridge import acquire
        bridged = acquire(rpc)
        item = {'tmdb_id': bridged['tmdb_id'], 'season': bridged['season'], 'episode': bridged['episode']}
        if current['season'] is not None and (item['season'], item['episode']) != (current['season'], current['episode']):
            raise Invalid('playing_episode_does_not_match_bridge')
        address = bridged['url']
    else:
        item = identity(current, profile)
        address = current['url']
    key = hashlib.sha256(address.encode()).hexdigest()
    if expected_file is not None and key != expected_file:
        raise Invalid('playing_file_changed_before_scan')
    clock = current.get('props', {}).get('totaltime', {})
    playback_duration = sum(clock.get(unit, 0) * factor for unit, factor in
                            (('hours', 3600), ('minutes', 60), ('seconds', 1), ('milliseconds', .001)))
    if catalogue_episodes is not None:
        from live_catalogue import episode_key
        if episode_key(item) not in catalogue_episodes or playback_duration < 60:
            raise Invalid('no_catalogue_cut_for_playing_episode')
    if hinted is None and use_provider_hints:
        hinted = provider_window(profile, key, item)
    reference = None
    alternate_references = []
    if blind_cache is not None:
        if auto_reference:
            selected = select_references((blind_cache, *extra_reference_caches), item)
            if not selected:
                raise Invalid('automatic_scan_reference_unavailable')
            if hinted is None:
                raise Invalid('automatic_scan_hint_unavailable')
            reference, *alternate_references = selected
        else:
            reference = blind_cache.reference(item['tmdb_id'], item['season'])
            if reference is None:
                raise Invalid('blind_scan_reference_unavailable')
            for cache in extra_reference_caches:
                alternate = cache.reference(item['tmdb_id'], item['season'])
                if alternate is None:
                    raise Invalid('alternate_reference_unavailable')
                if alternate['reference_id'] not in {r['reference_id'] for r in [reference, *alternate_references]}:
                    alternate_references.append(alternate)
        seconds = 40

    began = time.perf_counter()
    last_check = [0]

    def changed():
        now = time.monotonic()
        if now-last_check[0] < .75:
            return False
        last_check[0] = now
        try:
            state = snapshot(allow_missing_identity=use_bridge)
            buffering = rpc('XBMC.GetInfoBooleans', {'booleans': ['Player.Caching']}).get('Player.Caching', False)
            return buffering or any(state[k] != current[k] for k in ('player', 'url', 'season', 'episode'))
        except (Invalid, KeyError):
            return True

    reader = Reader(address, cancelled=changed, max_bytes=max_bytes, block_size=1048576)
    # Session-specific URL digest deliberately loses reuse when a signed URL changes.
    # Do not claim a stable provider-file fingerprint or download a whole stream to hash it.
    sample = None
    checked_windows = []
    window_match = None
    try:
        with relay(reader) as url, tempfile.TemporaryDirectory(prefix='mdm-live-audio-') as folder:
            windows = [(start, 40) for start in (90, 0, 45, 135)] if reference else [(offset, seconds)]
            if hinted and reference:
                windows = [hinted] + [window for window in windows if window != hinted]
            if auto_reference:
                windows = [hinted]
            for offset, seconds in windows:
                reader.check()
                pcm = Path(folder)/'opening.wav'
                run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1',
                     '-protocol_whitelist', 'http,tcp', '-ss', str(offset), '-i', url, '-map', '0:a:0', '-vn', '-sn', '-dn',
                     '-t', str(seconds), '-ac', '1', '-ar', '11025', '-c:a', 'pcm_s16le',
                     '-fs', str(11025*2*seconds+4096), '-y', str(pcm)], max(0.05, reader.deadline-time.monotonic()), 0)
                if reader.error:
                    raise Invalid(reader.error)
                with wave.open(str(pcm), 'rb') as audio:
                    duration = audio.getnframes()/audio.getframerate()
                raw = json.loads(run([str(fpcalc), '-algorithm', '2', '-raw', '-json', '-length', str(seconds), str(pcm)], 10))
                if duration < min(seconds-1, 30):
                    raise Invalid('opening_capture_incomplete')
                sample = Sample(item['episode'], key, tuple(raw['fingerprint']), min(seconds, duration))
                if changed():
                    raise Invalid('playback_changed_or_buffering')
                if reference:
                    if alternate_references or auto_reference:
                        from variant_decision import decide
                        window_match = decide([reference, *alternate_references], item['tmdb_id'], item['season'], sample,
                                              cross_season=auto_reference)
                    else:
                        window_match = match(reference, item['tmdb_id'], item['season'], sample)
                    checked_windows.append({'start_seconds': offset, 'duration_seconds': round(duration, 3),
                                            'status': window_match['status']})
                    if window_match['status'] != 'no_repeat':
                        break
    except Invalid as exc:
        metrics = {'elapsed_seconds': round(time.perf_counter()-began, 3),
                   'stream_payload_bytes': reader.bytes, 'upstream_requests': reader.requests,
                   'transport_error_type': reader.error_type, 'sample_start_seconds': offset,
                   'requested_sample_seconds': seconds, 'checked_windows': checked_windows}
        if reader.error and reader.error != 'capture_finished':
            exc = Invalid(reader.error)
        exc.metrics = metrics
        raise exc from None
    return item, sample, {'elapsed_seconds': round(time.perf_counter()-began, 3),
                         'playback_duration': playback_duration,
                         'stream_payload_bytes': reader.bytes, 'upstream_requests': reader.requests,
                         'analysed_seconds': round(duration, 3), 'sample_start_seconds': offset,
                         'collection_hint': 'provider_guided_search' if hinted and reference else 'labelled_chapters' if use_chapter_window else 'explicit_diagnostic_window' if offset else 'opening_prefix',
                         'identity_scope': 'current_signed_url_session',
                         **({'checked_windows': checked_windows, 'window_match': window_match} if reference else {}),
                         **({'reference_season': reference['season']} if auto_reference else {})}


class Session:
    def __init__(self, path):
        self.path = path

    def remember(self, item, sample):
        con = sqlite3.connect(self.path)
        try:
            if con.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise Invalid('session_future_schema')
            with con:
                con.execute('CREATE TABLE IF NOT EXISTS samples (show INTEGER, season INTEGER, episode INTEGER, id TEXT, words TEXT, seconds REAL, updated REAL, PRIMARY KEY(show,season,episode))')
                con.execute('PRAGMA user_version=1')
                con.execute('DELETE FROM samples WHERE updated<?', (time.time()-7*86400,))
                con.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?)',
                            (item['tmdb_id'], item['season'], sample.episode, sample.file_id,
                             json.dumps(sample.words), sample.seconds, time.time()))
                con.execute('DELETE FROM samples WHERE rowid NOT IN (SELECT rowid FROM samples ORDER BY updated DESC LIMIT 8)')
            rows = con.execute('SELECT episode,id,words,seconds FROM samples WHERE show=? AND season=? ORDER BY updated LIMIT 4',
                               (item['tmdb_id'], item['season'])).fetchall()
            return [Sample(ep, key, tuple(json.loads(words)), seconds) for ep, key, words, seconds in rows]
        finally:
            con.close()


def main():
    p = argparse.ArgumentParser(description='On-demand selected Kodi episode audio diagnostic; no playback control.')
    p.add_argument('--ffmpeg', type=Path, default=Path('C:/ffmpeg/bin/ffmpeg.exe'))
    p.add_argument('--fpcalc', type=Path, required=True)
    p.add_argument('--profile', type=Path, default=Path(os.environ.get('APPDATA', ''))/'Kodi/userdata/addon_data/plugin.video.mdm.flix')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=240)
    p.add_argument('--check', action='store_true', help='Read readiness only; do not fetch/decode stream data.')
    p.add_argument('--bridge', action='store_true', help='Use installed diagnostic script for resolved path and owned Flix identity.')
    p.add_argument('--chapter-window', action='store_true', help='Collect only a padded window around labelled intro chapters.')
    p.add_argument('--window-start', type=int, default=0, help='Explicit diagnostic playback offset; does not move Kodi playback.')
    p.add_argument('--blind-windows', action='store_true', help='Search fixed opening windows under one read budget.')
    p.add_argument('--reference-db', type=Path, action='append', default=[],
                   help='Compare an existing reference database. Repeat for alternate variants.')
    p.add_argument('--kodi-preview', action='store_true', help='Send a candidate to Flix for manual review.')
    p.add_argument('--timing-hint', type=float, nargs=2, metavar=('START', 'END'),
                   help='Try a provider timing window first. Times are playback seconds.')
    p.add_argument('--provider-hints', action='store_true',
                   help='Try the cached Flix timing window for the active file first.')
    p.add_argument('--auto-reference', action='store_true',
                   help='Select saved references for this show and scan one provider-guided window.')
    p.add_argument('--expected-file', help=argparse.SUPPRESS)
    source = p.add_mutually_exclusive_group()
    source.add_argument('--catalogue', type=Path, help='Match a local timing snapshot using one bounded opening scan.')
    source.add_argument('--public-catalogue', action='store_true', help='Use the cached public timing snapshot.')
    p.add_argument('--save-cut', type=Path, help='Save opening fingerprints for private review, without timing claims.')
    args = p.parse_args()
    if args.save_cut and not args.catalogue:
        p.error('--save-cut requires --catalogue')
    catalogue_mode = args.catalogue or args.public_catalogue
    if catalogue_mode and (not args.bridge or args.blind_windows or args.chapter_window or args.window_start):
        p.error('Catalogue lookup requires --bridge and a fixed opening scan')
    if args.blind_windows and (not args.bridge or args.chapter_window or args.window_start):
        p.error('--blind-windows requires --bridge and cannot use a chapter window or explicit offset')
    if len(args.reference_db) > 3 or any(not path.is_file() for path in args.reference_db):
        p.error('Supply up to three existing reference databases')
    if args.kodi_preview and (not args.bridge or len(args.reference_db) < (1 if args.auto_reference else 2)):
        p.error('--kodi-preview requires the bridge and reference databases')
    if (args.timing_hint is not None or args.provider_hints) and not args.blind_windows:
        p.error('Timing hints require --blind-windows')
    if args.auto_reference and (not args.blind_windows or not args.reference_db):
        p.error('--auto-reference requires --blind-windows and at least one --reference-db')
    report = {'schema': 1, 'automatic_skip_eligible': False}
    if args.check:
        try:
            item = identity(snapshot(), args.profile)
            report.update(status='ready_for_on_demand_capture', show_tmdb=item['tmdb_id'],
                          season=item['season'], episode=item['episode'])
        except Invalid as exc:
            report['status'] = str(exc)
        except (OSError, ValueError, TypeError, KeyError):
            report['status'] = 'fresh_matching_flix_report_required'
        print(json.dumps(report, indent=2))
        return
    try:
        if catalogue_mode:
            from live_catalogue import apply_capture, load_snapshot
            if args.public_catalogue:
                from catalogue_download import refresh
                catalogue = refresh(args.output / 'public-catalogue-cache.json')
            else:
                catalogue = load_snapshot(args.catalogue)
            if not catalogue or not catalogue[0]:
                print(json.dumps({'status': 'public_catalogue_empty_or_unavailable'}))
                return
            episodes = {cut.episode for cut in catalogue[0].values()}
            item, sample, metrics = capture(args.ffmpeg, args.fpcalc, args.profile, 30,
                                            use_bridge=True, expected_file=args.expected_file,
                                            catalogue_episodes=episodes)
            markers = apply_capture(args.profile, item, sample, metrics, catalogue)
            if args.save_cut:
                from live_catalogue import save_cut_draft
                save_cut_draft(args.save_cut, item, sample, metrics)
            print(json.dumps({'status': 'catalogue_match' if markers else 'catalogue_miss',
                              'markers': sorted(markers), 'metrics': metrics}))
            return
        caches = [Cache(path) for path in args.reference_db]
        primary = (caches[0] if caches else Cache(args.output/'references.sqlite')) if args.blind_windows else None
        item, sample, metrics = capture(args.ffmpeg, args.fpcalc, args.profile, args.seconds, use_bridge=args.bridge, use_chapter_window=args.chapter_window, window_start=args.window_start,
                                        blind_cache=primary, extra_reference_caches=caches[1:],
                                        timing_hint=args.timing_hint, use_provider_hints=args.provider_hints or args.auto_reference,
                                        auto_reference=args.auto_reference, expected_file=args.expected_file)
        args.output.mkdir(parents=True, exist_ok=True)
        report.update(show_tmdb=item['tmdb_id'], season=item['season'], episode=sample.episode, metrics=metrics)
        if args.blind_windows:
            report['match'] = metrics.pop('window_match')
            candidate = report['match'].get('candidate')
            if candidate:
                report['playback_candidate'] = {k: round(candidate[k]+metrics['sample_start_seconds'], 3) for k in ('start', 'end')}
            report['status'] = 'blind_window_scan_complete'
        elif caches:
            from variant_decision import decide
            references = [ref_cache.reference(item['tmdb_id'], item['season']) for ref_cache in caches]
            if any(ref is None for ref in references):
                raise Invalid('diagnostic_reference_unavailable')
            report['match'] = decide(references, item['tmdb_id'], item['season'], sample)
            candidate = report['match']['candidate']
            if candidate:
                report['playback_candidate'] = {k: round(candidate[k]+metrics['sample_start_seconds'], 3) for k in ('start', 'end')}
            report['status'] = 'variant_scan_complete'
        else:
            cache = Cache(args.output/'references.sqlite')
            previous = cache.reference(item['tmdb_id'], item['season'])
            if previous:
                report['match'] = match(previous, item['tmdb_id'], item['season'], sample)
                candidate = report['match'].get('candidate')
                if candidate:
                    report['playback_candidate'] = {k: round(candidate[k]+metrics['sample_start_seconds'], 3) for k in ('start', 'end')}
            samples = Session(args.output/'session.sqlite').remember(item, sample)
            report.update(status='audio_captured', distinct_episodes=len(samples))
            if len(samples) >= 3 and not previous:
                try:
                    reference = learn(item['tmdb_id'], item['season'], samples)
                    cache.save_reference(reference)
                    report['reference_status'] = 'provisional_reference_saved'
                except Invalid as exc:
                    report['reference_status'] = str(exc)
        if args.kodi_preview and report.get('playback_candidate'):
            result = report['match']
            digest = result.get('file_id') or result['matches'][0]['file_id']
            preview = dict(schema=1, file_id=digest, tmdb_id=item['tmdb_id'],
                           season=item['season'], episode=item['episode'], **report['playback_candidate'])
            args.profile.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=args.profile,
                                             prefix='.audio-intro-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(preview, stream)
            try:
                temporary.replace(args.profile/'audio_intro_candidate.json')
            finally:
                temporary.unlink(missing_ok=True)
            report['kodi_preview_saved'] = True
    except Invalid as exc:
        report['status'] = str(exc)
        if hasattr(exc, 'metrics'):
            report['metrics'] = exc.metrics
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        report['status'] = 'capture_or_storage_unavailable'
    print(json.dumps(report, indent=2))
    if args.output.is_dir():
        (args.output/'last_live_capture.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    main()
