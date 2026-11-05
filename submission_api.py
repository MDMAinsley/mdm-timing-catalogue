"""Private contribution inbox. Unreviewed entries never become public timings."""
import hashlib
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from vote_store import Votes
from review_registry import Registry
from cut_evidence import normalize

HEX = re.compile(r'[0-9a-f]{64}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
METHOD = 'chromaprint2-fixed-32-v1'


class Rejected(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise Rejected(400, 'invalid_fields')


def validate_proposal(body):
    if not isinstance(body, dict):
        raise Rejected(400, 'invalid_fields')
    extra = set(body) - set('episode duration anchors marker start end origin'.split())
    if extra - {'assessment', 'reference', 'operation'}:
        raise Rejected(400, 'invalid_fields')
    fields({k:v for k,v in body.items() if k not in extra}, 'episode duration anchors marker start end origin')
    episode, duration = body['episode'], body['duration']
    if (not isinstance(episode, list) or len(episode) != 3
            or any(type(part) is not int for part in episode)
            or not 1 <= episode[0] <= 100000000 or not 0 <= episode[1] <= 1000
            or not 1 <= episode[2] <= 10000):
        raise Rejected(400, 'invalid_episode')
    if any(type(body[k]) not in (int, float) or not math.isfinite(body[k])
           for k in ('duration', 'start', 'end')):
        raise Rejected(400, 'invalid_times')
    if not 60 <= duration <= 86400 or not 0 <= body['start'] < body['end'] <= duration:
        raise Rejected(400, 'invalid_range')
    anchors = body['anchors']
    if not isinstance(anchors,list):
        raise Rejected(400, 'invalid_anchors')
    if anchors:
        try:
            normalize(anchors)
        except ValueError:
            raise Rejected(400,'invalid_anchors')
    if body['marker'] not in ('intro', 'outro') or body['origin'] not in ('manual', 'software', 'chapters'):
        raise Rejected(400, 'invalid_marker')
    # Normalize numeric representations and anchor order for retry deduplication.
    result = dict(episode=episode, duration=float(duration), anchors=sorted(anchors),
                marker=body['marker'], start=float(body['start']), end=float(body['end']),
                origin=body['origin'])
    if 'assessment' in body:
        if body['assessment'] not in ('good', 'bad', 'correction'):
            raise Rejected(400, 'invalid_assessment')
        result['assessment'] = body['assessment']
    if 'reference' in body:
        reference = body['reference']
        fields(reference, 'marker revision')
        if (not isinstance(reference['marker'], str) or not HEX.fullmatch(reference['marker'])
                or type(reference['revision']) is not int or reference['revision'] < 1):
            raise Rejected(400, 'invalid_reference')
        result['reference'] = reference
    if 'operation' in body:
        operation=body['operation']
        if not isinstance(operation,str) or not re.fullmatch('[0-9a-f]{32}',operation):
            raise Rejected(400,'invalid_operation')
        result['operation']=operation
    return result


class App:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.votes = Votes(self.directory / 'votes.sqlite')
        self.registry = Registry(self.directory)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS credentials (
                    hash TEXT PRIMARY KEY, created INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, actor INTEGER NOT NULL, payload TEXT NOT NULL,
                    created INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS limits (
                    actor INTEGER NOT NULL, bucket INTEGER NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(actor,bucket)
                );
                CREATE TABLE IF NOT EXISTS ingestion (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, receipt TEXT NOT NULL UNIQUE
                );
            ''')
            db.execute('INSERT OR IGNORE INTO ingestion(receipt) SELECT id FROM inbox ORDER BY created,rowid')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.directory / 'inbox.sqlite', timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def actor(self, environ):
        auth = environ.get('HTTP_AUTHORIZATION', '')
        if not auth.startswith('Bearer ') or not TOKEN.fullmatch(auth[7:]):
            raise Rejected(401, 'authentication_required')
        key = digest(auth[7:])
        with self.db() as db:
            row = db.execute('SELECT revoked FROM credentials WHERE hash=?', (key,)).fetchone()
        if not row or row['revoked']:
            raise Rejected(401, 'invalid_credential')
        actor=self.votes.anonymous(key)
        with self.votes.db() as db:
            identity=db.execute('SELECT linked_to FROM actors WHERE id=?',(actor,)).fetchone()
        if identity['linked_to'] is not None:
            raise Rejected(401,'linked_credential_inactive')
        return actor

    def limit(self, actor):
        bucket = int(time.time()) // 3600
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM limits WHERE bucket < ?', (bucket - 48,))
            count = db.execute('SELECT count FROM limits WHERE actor=? AND bucket=?', (actor, bucket)).fetchone()
            if count and count['count'] >= 60:
                raise Rejected(429, 'hourly_limit')
            db.execute('INSERT INTO limits VALUES(?,?,1) ON CONFLICT(actor,bucket) DO UPDATE SET count=count+1',
                       (actor, bucket))

    def handle(self, environ):
        path = environ.get('PATH_INFO', '')
        method = environ.get('REQUEST_METHOD', '')
        if path == '/health' and method == 'GET':
            return 200, dict(service='MDM Skip', status='ready', api=1, publication='manual review', github_sign_in=False)
        if path not in ('/v1/contributors', '/v1/proposals', '/v1/votes'):
            raise Rejected(404, 'not_found')
        if method != 'POST':
            raise Rejected(405, 'post_required')
        if environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
            raise Rejected(415, 'json_required')
        try:
            length = int(environ.get('CONTENT_LENGTH', '0'))
        except ValueError:
            raise Rejected(400, 'invalid_length')
        if not 1 <= length <= 8192:
            raise Rejected(413, 'invalid_body_size')
        try:
            body = json.loads(environ['wsgi.input'].read(length))
        except (ValueError, UnicodeError):
            raise Rejected(400, 'invalid_json')
        if path == '/v1/contributors':
            fields(body, 'consent')
            if body['consent'] is not True:
                raise Rejected(400, 'consent_required')
            token = secrets.token_urlsafe(32)
            with self.db() as db:
                db.execute('INSERT INTO credentials(hash,created) VALUES(?,?)', (digest(token), int(time.time())))
            return 201, dict(token=token, cohort='anonymous', eligible_for_promotion=False)
        actor = self.actor(environ)
        self.limit(actor)
        if path == '/v1/proposals':
            proposal = validate_proposal(body)
            payload = json.dumps(proposal, sort_keys=True, separators=(',', ':'), allow_nan=False)
            identity = digest(str(actor) + ':' + (proposal['operation'] if 'operation' in proposal else payload))
            state = 'pending_review' if proposal['anchors'] else 'pending_cut_verification'
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT payload FROM inbox WHERE id=?',(identity,)).fetchone()
                if old and old['payload']!=payload:
                    raise Rejected(400,'operation_already_used')
                db.execute('INSERT OR IGNORE INTO inbox(id,actor,payload,created,state) VALUES(?,?,?,?,?)',
                           (identity, actor, payload, int(time.time()), state))
                db.execute('INSERT OR IGNORE INTO ingestion(receipt) VALUES(?)',(identity,))
                sequence=db.execute('SELECT sequence FROM ingestion WHERE receipt=?',(identity,)).fetchone()[0]
            vote=self.registry.apply(actor,identity,sequence,proposal)
            if vote:
                with self.db() as db:
                    db.execute("UPDATE inbox SET state='feedback_recorded' WHERE id=?",(identity,))
            return 202, dict(receipt=identity, state=state, published=False, counted_vote=bool(vote and vote['applied']),
                             eligible_for_promotion=self.registry.eligible(actor))
        fields(body, 'target good')
        if not isinstance(body['target'], str) or not HEX.fullmatch(body['target']) or type(body['good']) is not bool:
            raise Rejected(400, 'invalid_vote')
        try:
            if not self.registry.active(body['target']):
                raise ValueError('inactive_target')
            self.votes.cast(actor, body['target'], body['good'])
        except ValueError:
            raise Rejected(404, 'unknown_or_inactive_target')
        return 200, dict(state='recorded', cohort='anonymous', eligible_for_promotion=self.registry.eligible(actor))

    def __call__(self, environ, start_response):
        try:
            status, body = self.handle(environ)
        except Rejected as error:
            status, body = error.status, dict(error=error.code)
        except Exception:
            # Do not leak request bodies, credentials, database paths or stack traces.
            status, body = 503, dict(error='temporarily_unavailable')
        payload = json.dumps(body, separators=(',', ':')).encode()
        descriptions = {200:'OK',201:'Created',202:'Accepted',400:'Bad Request',401:'Unauthorized',
                        404:'Not Found',405:'Method Not Allowed',413:'Content Too Large',
                        415:'Unsupported Media Type',429:'Too Many Requests',503:'Service Unavailable'}
        start_response(f'{status} {descriptions[status]}', [('Content-Type','application/json'),
                       ('Content-Length',str(len(payload))),('Cache-Control','no-store')])
        return [payload]


def create_app():
    return App(os.environ.get('MDM_SKIP_DATA', '/var/lib/mdm-skip'))
