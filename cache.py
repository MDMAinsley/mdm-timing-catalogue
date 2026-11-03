"""Small local research cache. No paths, media bytes, tokens or URLs."""
import json
import sqlite3
import time

from detector import Invalid
from references import METHOD, encoded, validate

REF_LIMIT = 64
RESULT_LIMIT = 256
REF_TTL = 30*86400
RESULT_TTL = 7*86400


class Cache:
    def __init__(self, path, now=time.time):
        self.path, self.now = path, now

    def connect(self):
        con = sqlite3.connect(self.path, timeout=.25)
        version = con.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            con.close()
            raise Invalid('cache_future_schema')
        try:
            if version == 0:
                con.execute('CREATE TABLE IF NOT EXISTS refs (k TEXT PRIMARY KEY, data BLOB, expires REAL, used REAL)')
                con.execute('CREATE TABLE IF NOT EXISTS results (k TEXT PRIMARY KEY, data BLOB, expires REAL, used REAL)')
                con.execute('PRAGMA user_version=1')
                con.commit()
            return con
        except BaseException:
            con.close()
            raise

    def put(self, table, key, value):
        if table not in ('refs', 'results'):
            raise Invalid('invalid_cache_table')
        data = encoded(value)
        limit, ttl, cap = (REF_LIMIT, REF_TTL, 65536) if table == 'refs' else (RESULT_LIMIT, RESULT_TTL, 8192)
        if len(data) > cap:
            raise Invalid('cache_payload_limit')
        con = self.connect()
        try:
            with con:
                con.execute('PRAGMA max_page_count=4096')  # ~16 MB on default 4 KiB pages
                for name in ('refs', 'results'):
                    con.execute('DELETE FROM '+name+' WHERE expires<=?', (self.now(),))
                con.execute('INSERT OR REPLACE INTO '+table+' VALUES (?,?,?,?)',
                            (key, data, self.now()+ttl, self.now()))
                con.execute('DELETE FROM '+table+' WHERE k NOT IN (SELECT k FROM '+table+' ORDER BY used DESC,k DESC LIMIT ?)', (limit,))
        finally:
            con.close()

    def get(self, table, key):
        if table not in ('refs', 'results'):
            raise Invalid('invalid_cache_table')
        con = self.connect()
        try:
            row = con.execute('SELECT data,used FROM '+table+' WHERE k=? AND expires>?', (key,self.now())).fetchone()
            if not row:
                return None
            cap = 65536 if table == 'refs' else 8192
            if len(row[0]) > cap:
                raise Invalid('cache_payload_limit')
            result = json.loads(row[0])
            # Coarse LRU: at most one touch/hour. Warm playback avoids needless disk writes.
            if row[1] < self.now()-3600:
                with con:
                    con.execute('UPDATE '+table+' SET used=? WHERE k=?', (self.now(), key))
            return result
        finally:
            con.close()

    def save_reference(self, ref):
        validate(ref)
        self.put('refs', f"{ref['show_tmdb']}:{ref['season']}", ref)

    def reference(self, show, season):
        value = self.get('refs', f'{show}:{season}')
        return validate(value) if value is not None else None

    def nearby_reference(self, show, season):
        """Return an adjacent-season reference for the same show, if available."""
        for other in (season - 1, season + 1):
            if other >= 1:
                reference = self.reference(show, other)
                if reference is not None:
                    return reference
        return None

    @staticmethod
    def key(ref, episode, file_id, seconds):
        return f"{METHOD}:{ref['show_tmdb']}:{ref['season']}:{episode}:{file_id}:{ref['reference_id']}:{seconds}"

    def save_result(self, ref, target, seconds, result):
        self.put('results', self.key(ref, target.episode, target.file_id, seconds), result)

    def result(self, ref, episode, file_id, seconds):
        value = self.get('results', self.key(ref, episode, file_id, seconds))
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != {'schema', 'method', 'show_tmdb', 'season', 'episode',
                'file_id', 'reference_id', 'status', 'candidate', 'automatic_skip_eligible', 'note'} or
                value.get('note') != 'Reference audio match is provisional; semantic identity and boundaries remain unvalidated.' or
                type(value.get('schema')) is not int or value['schema'] != 1 or
                value.get('automatic_skip_eligible') is not False or
                (value.get('show_tmdb'), value.get('season'), value.get('episode'), value.get('file_id'),
                 value.get('reference_id'), value.get('method')) !=
                (ref['show_tmdb'], ref['season'], episode, file_id, ref['reference_id'], METHOD)):
            raise Invalid('invalid_cached_result')
        state, candidate = value.get('status'), value.get('candidate')
        if state not in ('no_repeat', 'provisional_repeat', 'ambiguous_repeats'):
            raise Invalid('invalid_cached_result')
        if state == 'provisional_repeat':
            import math
            if not isinstance(candidate, dict) or set(candidate) != {'start', 'end', 'supporting_episodes', 'mean_bits'}:
                raise Invalid('invalid_cached_result')
            start, end = candidate.get('start'), candidate.get('end')
            if any(type(n) not in (float, int) or not math.isfinite(n) for n in (start, end)) or not 0 <= start < end <= seconds:
                raise Invalid('invalid_cached_result')
            peers, bits = candidate['supporting_episodes'], candidate['mean_bits']
            if (not isinstance(peers, list) or not 2 <= len(peers) <= 3 or
                    any(type(v) is not int or not 1 <= v <= 10000 for v in peers) or
                    len(set(peers)) != len(peers) or episode in peers or
                    type(bits) not in (int, float) or not math.isfinite(bits) or not 0 <= bits <= 4):
                raise Invalid('invalid_cached_result')
        elif candidate is not None:
            raise Invalid('invalid_cached_result')
        return value
