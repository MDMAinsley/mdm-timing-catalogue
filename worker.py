"""Local-only, finite CLI experiment. Does not start a service or access Kodi."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import wave

from detector import Invalid, Sample, analyse

EXTENSIONS = {'.wav', '.flac', '.mp3', '.m4a', '.aac', '.ogg', '.mp4', '.mkv', '.mov', '.avi', '.webm', '.ts'}
MAX_PCM = 11025 * 2 * 480 + 4096


def run(command, timeout, max_output=100000):
    """No console windows, child shell, raw stderr, or unbounded captured output."""
    with tempfile.TemporaryFile() as out:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL, timeout=timeout,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise Invalid('analysis_timeout') from None
        except OSError:
            raise Invalid('tool_unavailable') from None
        if result.returncode:
            raise Invalid('media_decode_or_fingerprint_failed')
        if out.tell() > max_output:
            raise Invalid('tool_output_limit')
        out.seek(0)
        return out.read()


def validate(job):
    if not isinstance(job, dict) or type(job.get('schema')) is not int or job['schema'] != 1:
        raise Invalid('invalid_request')
    for key, minimum in (('show_tmdb', 1), ('season', 0)):
        v = job.get(key)
        if type(v) is not int or not minimum <= v <= 10000000:
            raise Invalid('invalid_show_or_season')
    rows = job.get('episodes')
    if not isinstance(rows, list) or not 3 <= len(rows) <= 4:
        raise Invalid('need_three_or_four_distinct_episodes')
    seen, paths = set(), set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get('episode')) is not int or not 1 <= row['episode'] <= 10000:
            raise Invalid('invalid_episode')
        if row['episode'] in seen:
            raise Invalid('duplicate_episode_or_file')
        seen.add(row['episode'])
        value = row.get('path')
        if not isinstance(value, str) or '://' in value or value.startswith(('\\\\', '//')):
            raise Invalid('local_regular_files_only')
        p = Path(value).resolve()
        if not p.is_file() or p.suffix.lower() not in EXTENSIONS or p in paths:
            raise Invalid('invalid_or_duplicate_local_file')
        paths.add(p)
    return rows


def local_identity(path, timeout=60):
    began = time.monotonic()
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024*1024):
            digest.update(block)
            if time.monotonic()-began > timeout:
                raise Invalid('analysis_timeout')
    return digest.hexdigest()


def extract(row, ffmpeg, fpcalc, seconds=480, timeout=60, known_identity=None):
    if type(seconds) is not int or not 30 <= seconds <= 480:
        raise Invalid('invalid_opening_limit')
    path = Path(row['path']).resolve()
    began = time.monotonic()
    # Full content hash is local disk work, not a stream download. No weak stat identity.
    identity = known_identity or local_identity(path, timeout)
    with tempfile.TemporaryDirectory(prefix='mdm-audio-') as folder:
        pcm = Path(folder)/'opening.wav'
        run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1',
             '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:a:0',
             '-vn', '-sn', '-dn', '-t', str(seconds), '-ac', '1', '-ar', '11025',
             '-c:a', 'pcm_s16le', '-fs', str(MAX_PCM), '-y', str(pcm)],
            max(.01, timeout-(time.monotonic()-began)), max_output=0)
        if pcm.stat().st_size > MAX_PCM:
            raise Invalid('decoded_audio_limit')
        with wave.open(str(pcm), 'rb') as wav:
            duration = wav.getnframes()/wav.getframerate()
            pcm_bytes = wav.getnframes()*wav.getsampwidth()*wav.getnchannels()
        try:
            raw = json.loads(run([str(fpcalc), '-algorithm', '2', '-raw', '-json',
                                  '-length', str(seconds), str(pcm)],
                                 max(.01, timeout-(time.monotonic()-began))))
            sample = Sample(row['episode'], identity, tuple(raw['fingerprint']), min(duration, seconds))
        except (ValueError, KeyError, TypeError):
            raise Invalid('invalid_fingerprint') from None
    return sample, {'episode': row['episode'], 'analysed_seconds': round(duration, 3),
                    'decoded_pcm_bytes': pcm_bytes, 'elapsed_seconds': round(time.monotonic()-began, 3)}


def process(job, ffmpeg, fpcalc, seconds=480):
    rows = validate(job)
    samples, metrics = [], []
    for row in rows:
        sample, metric = extract(row, ffmpeg, fpcalc, seconds)
        samples.append(sample)
        metrics.append(metric)
    began = time.monotonic()
    report = analyse(samples, cancelled=lambda: time.monotonic()-began > 20)
    report.update(show_tmdb=job['show_tmdb'], season=job['season'], metrics=metrics,
                  comparison_seconds=round(time.monotonic()-began, 3))
    return report


def main():
    p = argparse.ArgumentParser(description='Experimental local repeated-opening audio detection; no Kodi changes.')
    p.add_argument('request', type=Path)
    p.add_argument('--ffmpeg', required=True, type=Path)
    p.add_argument('--fpcalc', required=True, type=Path)
    p.add_argument('--seconds', type=int, default=480)
    args = p.parse_args()
    try:
        if args.request.stat().st_size > 32768:
            raise Invalid('request_too_large')
        report = process(json.loads(args.request.read_text(encoding='utf8')), args.ffmpeg, args.fpcalc, args.seconds)
    except KeyboardInterrupt:
        report = {'schema': 1, 'status': 'cancelled', 'automatic_skip_eligible': False}
    except (Invalid, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, Invalid) else 'invalid_request_or_local_io'
        report = {'schema': 1, 'status': code, 'automatic_skip_eligible': False}
    print(json.dumps(report, indent=2))
    return 0 if 'results' in report else 1


if __name__ == '__main__':
    raise SystemExit(main())
