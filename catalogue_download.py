"""Refresh the published snapshot at most daily and retain the last valid copy."""
import os
from pathlib import Path
import tempfile
import time
from urllib.request import urlopen

from live_catalogue import load_snapshot

URL = 'https://raw.githubusercontent.com/MDMAinsley/mdm-timing-catalogue/main/playback-snapshot.json'
LIMIT = 2_000_000
REFRESH_SECONDS = 86400
RETRY_SECONDS = 900
INVALID = (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError)


def read_public(path):
    snapshot = load_snapshot(path)
    if len(snapshot) != 2:
        raise ValueError('public_catalogue_requires_compiled_snapshot')
    return snapshot


def refresh(cache, *, now=None):
    now = time.time() if now is None else now
    cache = Path(cache)
    retry = cache.with_suffix('.attempt')
    try:
        previous = read_public(cache)
        if 0 <= now - cache.stat().st_mtime < REFRESH_SECONDS:
            return previous
    except INVALID:
        previous = None

    temporary = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        if retry.exists() and 0 <= now - retry.stat().st_mtime < RETRY_SECONDS:
            return previous
        retry.touch()
        with urlopen(URL, timeout=5) as response:
            if response.geturl() != URL:
                raise ValueError('unexpected_catalogue_redirect')
            payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError('catalogue_snapshot_too_large')
        with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        snapshot = read_public(temporary)
        os.replace(temporary, cache)
        return snapshot
    except INVALID:
        return previous
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
