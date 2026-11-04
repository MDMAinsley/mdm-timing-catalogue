"""Private SQLite vote storage for the future submission service."""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from catalogue import Review
from vote_summary import AnonymousReview, summary

HEX = re.compile('[0-9a-f]{64}')


class Votes:
    """Identity methods are internal; HTTP callers must never choose roles."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=5)
        database.row_factory = sqlite3.Row
        try:
            database.execute('PRAGMA foreign_keys=ON')
            version = database.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('newer_vote_database')
            database.executescript("""
                CREATE TABLE IF NOT EXISTS actors (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('anonymous','signed_in')),
                    identity TEXT NOT NULL,
                    linked_to INTEGER REFERENCES actors(id),
                    UNIQUE(kind,identity)
                );
                CREATE TABLE IF NOT EXISTS records (
                    record TEXT PRIMARY KEY,
                    cut TEXT NOT NULL,
                    marker TEXT NOT NULL CHECK(marker IN ('intro','outro'))
                );
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    record TEXT NOT NULL REFERENCES records(record),
                    revision INTEGER NOT NULL CHECK(revision>0),
                    start REAL NOT NULL,
                    end REAL NOT NULL,
                    duration REAL NOT NULL,
                    UNIQUE(record,revision)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor INTEGER NOT NULL REFERENCES actors(id),
                    target TEXT NOT NULL REFERENCES targets(id),
                    good INTEGER NOT NULL CHECK(good IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS votes (
                    actor INTEGER NOT NULL REFERENCES actors(id),
                    target TEXT NOT NULL REFERENCES targets(id),
                    good INTEGER NOT NULL CHECK(good IN (0,1)),
                    event INTEGER NOT NULL REFERENCES events(id),
                    PRIMARY KEY(actor,target)
                );
                PRAGMA user_version=1;
            """)
            database.execute('BEGIN IMMEDIATE')
            yield database
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()

    def anonymous(self, contributor_hash):
        if not isinstance(contributor_hash, str) or not HEX.fullmatch(contributor_hash):
            raise ValueError('invalid_contributor_hash')
        return self._actor('anonymous', contributor_hash)

    def signed_in(self, verified_github_id):
        if type(verified_github_id) is not int or verified_github_id < 1:
            raise ValueError('invalid_verified_account')
        return self._actor('signed_in', str(verified_github_id))

    def _actor(self, kind, identity):
        with self.db() as database:
            database.execute('INSERT OR IGNORE INTO actors(kind,identity) VALUES(?,?)', (kind, identity))
            return database.execute('SELECT id FROM actors WHERE kind=? AND identity=?',
                                    (kind, identity)).fetchone()['id']

    def target(self, cut, marker, record, revision, start, end, duration):
        if (not isinstance(cut, str) or not HEX.fullmatch(cut)
                or marker not in ('intro', 'outro') or not isinstance(record, str)
                or not 1 <= len(record) <= 128 or type(revision) is not int or revision < 1
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (start, end, duration))
                or not 0 <= start < end <= duration or not 60 <= duration <= 86400):
            raise ValueError('invalid_vote_target')
        identity = [cut, marker, record, revision]
        key = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        with self.db() as database:
            prior = database.execute('SELECT * FROM records WHERE record=?', (record,)).fetchone()
            if prior and (prior['cut'] != cut or prior['marker'] != marker):
                raise ValueError('record_belongs_to_different_cut_or_marker')
            database.execute('INSERT OR IGNORE INTO records VALUES(?,?,?)', (record, cut, marker))
            prior = database.execute('SELECT * FROM targets WHERE id=?', (key,)).fetchone()
            if prior and tuple(prior[field] for field in ('start', 'end', 'duration')) != (start, end, duration):
                raise ValueError('timing_revision_is_immutable')
            database.execute('INSERT OR IGNORE INTO targets VALUES(?,?,?,?,?,?)',
                             (key, record, revision, start, end, duration))
        return key

    def cast(self, actor, target, good):
        if type(actor) is not int or type(good) is not bool:
            raise ValueError('invalid_vote')
        with self.db() as database:
            identity = database.execute('SELECT * FROM actors WHERE id=?', (actor,)).fetchone()
            if not identity or identity['linked_to'] is not None:
                raise ValueError('invalid_or_linked_guest_identity')
            if not database.execute('SELECT 1 FROM targets WHERE id=?', (target,)).fetchone():
                raise ValueError('unknown_vote_target')
            previous = database.execute('SELECT good FROM votes WHERE actor=? AND target=?',
                                        (actor, target)).fetchone()
            if previous and previous['good'] == good:
                return
            event = database.execute('INSERT INTO events(actor,target,good) VALUES(?,?,?)',
                                     (actor, target, good)).lastrowid
            database.execute("""
                INSERT INTO votes VALUES(?,?,?,?)
                ON CONFLICT(actor,target) DO UPDATE SET good=excluded.good,event=excluded.event
            """, (actor, target, good, event))

    def link(self, guest, signed_in):
        """Call only after verifying possession of both identities."""
        with self.db() as database:
            source = database.execute('SELECT * FROM actors WHERE id=?', (guest,)).fetchone()
            destination = database.execute('SELECT * FROM actors WHERE id=?', (signed_in,)).fetchone()
            if (not source or not destination or source['kind'] != 'anonymous'
                    or destination['kind'] != 'signed_in' or destination['linked_to'] is not None
                    or source['linked_to'] not in (None, signed_in)):
                raise ValueError('invalid_identity_link')
            if source['linked_to'] == signed_in:
                return
            rows = database.execute('SELECT * FROM votes WHERE actor=?', (guest,)).fetchall()
            for row in rows:
                # An existing signed-in vote takes precedence during migration.
                if not database.execute('SELECT 1 FROM votes WHERE actor=? AND target=?',
                                        (signed_in, row['target'])).fetchone():
                    event = database.execute('INSERT INTO events(actor,target,good) VALUES(?,?,?)',
                                             (signed_in, row['target'], row['good'])).lastrowid
                    database.execute('INSERT INTO votes VALUES(?,?,?,?)',
                                     (signed_in, row['target'], row['good'], event))
            database.execute('UPDATE actors SET linked_to=? WHERE id=?', (signed_in, guest))
            database.execute('DELETE FROM votes WHERE actor=?', (guest,))

    def totals(self, target):
        signed_in, anonymous = [], []
        with self.db() as database:
            revision = database.execute('SELECT record,revision FROM targets WHERE id=?', (target,)).fetchone()
            if not revision:
                raise ValueError('unknown_vote_target')
            rows = database.execute("""
                SELECT actors.kind,actors.identity,votes.good FROM votes
                JOIN actors ON actors.id=votes.actor
                WHERE votes.target=? AND actors.linked_to IS NULL
            """, (target,)).fetchall()
            for row in rows:
                args = (revision['record'], revision['revision'], bool(row['good']))
                if row['kind'] == 'signed_in':
                    signed_in.append(Review(int(row['identity']), *args))
                else:
                    anonymous.append(AnonymousReview(row['identity'], *args))
        return summary(signed_in, anonymous, revision['record'], revision['revision'])
