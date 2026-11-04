"""Private, administrator-controlled cut registry and repeat-safe feedback bridge.

Admission and trust methods are for authenticated server administration, never
HTTP request roles. Receiving a contribution cannot admit a cut or grant trust.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time

from catalogue import Cut, Proposal, Review, match_cut
from export_catalogue import cut_identifier, marker_identifier
from vote_store import Votes
from vote_summary import AnonymousReview


def key(value):
    return hashlib.sha256(json.dumps(value,separators=(',', ':')).encode()).hexdigest()


class Registry:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.votes=Votes(self.directory/'votes.sqlite')

    @contextmanager
    def db(self):
        with self.votes.db() as db:
            statements = (
                '''CREATE TABLE IF NOT EXISTS catalogue_cuts (
                    id TEXT PRIMARY KEY, episode TEXT NOT NULL, duration REAL NOT NULL, anchors TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS catalogue_markers (
                    target TEXT PRIMARY KEY REFERENCES targets(id), software INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','superseded','withdrawn')),
                    public_id TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS catalogue_policy (
                    actor INTEGER PRIMARY KEY REFERENCES actors(id), eligible INTEGER NOT NULL,
                    trusted INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS catalogue_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, subject TEXT NOT NULL,
                    operator TEXT NOT NULL, created INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS catalogue_ingested (
                    receipt TEXT PRIMARY KEY, sequence INTEGER NOT NULL, actor INTEGER NOT NULL,
                    target TEXT NOT NULL, applied INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS catalogue_clock (
                    actor INTEGER NOT NULL, target TEXT NOT NULL, sequence INTEGER NOT NULL,
                    PRIMARY KEY(actor,target))''',
            )
            for statement in statements:
                db.execute(statement)
            yield db

    @staticmethod
    def _cuts(db):
        return {r['id']:Cut(tuple(json.loads(r['episode'])),r['duration'],
                           tuple(tuple(a) for a in json.loads(r['anchors'])))
                for r in db.execute('SELECT * FROM catalogue_cuts')}

    @staticmethod
    def _operator(operator):
        if not isinstance(operator,str) or not 1 <= len(operator) <= 80 or any(ord(c)<32 for c in operator):
            raise ValueError('authenticated_operator_required')

    @staticmethod
    def _audit(db,action,subject,operator):
        Registry._operator(operator)
        db.execute('INSERT INTO catalogue_audit(action,subject,operator,created) VALUES(?,?,?,?)',
                   (action,subject,operator,int(time.time())))

    def admit(self, body, *, confirmed_cut=False, software=False, operator=''):
        """Human administration must verify the evidence belongs to the stated edit.

        This does not approve the marker or grant a contributor eligibility.
        One canonical marker history per cut/kind has immutable revisions.
        """
        from submission_api import validate_proposal
        body=validate_proposal(body)
        self._operator(operator)
        if confirmed_cut is not True or len(body['anchors'])!=2 or type(software) is not bool:
            raise ValueError('verified_cut_with_audio_evidence_required')
        observed=Cut(tuple(body['episode']),float(body['duration']),tuple(tuple(a) for a in body['anchors']))
        with self.db() as db:
            cuts=self._cuts(db)
            cut_id=match_cut(observed,cuts)
            near=[c for c in cuts.values() if c.episode==observed.episode and set(c.anchors)==set(observed.anchors)
                  and abs(c.duration-observed.duration)<=3]
            if near and not cut_id:
                raise ValueError('ambiguous_cut_evidence')
            if not cut_id:
                cut_id=cut_identifier(observed)
                db.execute('INSERT INTO catalogue_cuts VALUES(?,?,?,?)',
                           (cut_id,json.dumps(observed.episode),observed.duration,json.dumps(observed.anchors)))
            cut=cuts.get(cut_id,observed)
            if body['end']>cut.duration:
                raise ValueError('marker_outside_canonical_runtime')
            record=key([cut_id,body['marker']])
            latest=db.execute('''SELECT targets.*,catalogue_markers.state,catalogue_markers.software
                FROM targets JOIN catalogue_markers ON targets.id=catalogue_markers.target
                WHERE record=? ORDER BY revision DESC LIMIT 1''',(record,)).fetchone()
            if (latest and latest['state']=='active' and latest['start']==body['start']
                    and latest['end']==body['end']):
                if latest['software']!=software:
                    raise ValueError('existing_revision_provenance_is_immutable')
                return latest['id']
            revision=latest['revision']+1 if latest else 1
            target=key([cut_id,body['marker'],record,revision])
            self.votes._target(db,target,cut_id,body['marker'],record,revision,
                               body['start'],body['end'],cut.duration)
            db.execute("UPDATE catalogue_markers SET state='superseded' WHERE target IN (SELECT id FROM targets WHERE record=?) AND state='active'",(record,))
            db.execute('INSERT INTO catalogue_markers VALUES(?,?,?,?)',
                       (target,int(software),'active',marker_identifier(cut_id,body['marker'],body['start'],body['end'])))
            self._audit(db,'admit_software' if software else 'admit_marker',target,operator)
            return target

    def policy(self, actor, *, eligible=False, trusted=False, operator=''):
        self._operator(operator)
        if type(eligible) is not bool or type(trusted) is not bool:
            raise ValueError('invalid_policy')
        with self.db() as db:
            identity=db.execute('SELECT * FROM actors WHERE id=?',(actor,)).fetchone()
            if not identity or identity['linked_to'] is not None or trusted and identity['kind']!='signed_in':
                raise ValueError('trusted_review_requires_verified_account')
            db.execute('INSERT OR REPLACE INTO catalogue_policy VALUES(?,?,?)',(actor,int(eligible),int(trusted)))
            self._audit(db,'policy_'+str(int(eligible))+'_'+str(int(trusted)),str(actor),operator)

    def review(self,target,reviewer,good,*,operator=''):
        with self.db() as db:
            role=db.execute('SELECT trusted FROM catalogue_policy WHERE actor=?',(reviewer,)).fetchone()
            if not role or not role['trusted'] or type(good) is not bool:
                raise ValueError('trusted_review_required')
            if not self._active(db,target):
                raise ValueError('inactive_target')
            self.votes._cast(db,reviewer,target,good)
            self._audit(db,'trusted_good' if good else 'trusted_bad',target,operator)

    def withdraw(self,target,*,operator=''):
        with self.db() as db:
            if not self._active(db,target):
                raise ValueError('inactive_target')
            db.execute("UPDATE catalogue_markers SET state='withdrawn' WHERE target=?",(target,))
            self._audit(db,'withdraw',target,operator)

    @staticmethod
    def _active(db,target):
        row=db.execute('SELECT state FROM catalogue_markers WHERE target=?',(target,)).fetchone()
        return bool(row and row['state']=='active')

    def active(self,target):
        with self.db() as db:
            return self._active(db,target)

    def eligible(self,actor):
        with self.db() as db:
            row=db.execute('''SELECT catalogue_policy.eligible,actors.linked_to FROM catalogue_policy
                JOIN actors ON catalogue_policy.actor=actors.id WHERE actors.id=?''',(actor,)).fetchone()
            return bool(row and row['eligible'] and row['linked_to'] is None)

    def apply(self,actor,receipt,sequence,body):
        """Apply one operation once, using server ingestion order, to one exact cut.

        Legacy payload-only receipts lack operation identity and remain pending.
        Client-supplied reference IDs never substitute for matched cut evidence.
        """
        if body.get('assessment') not in ('good','bad') or not body.get('operation') or len(body['anchors'])!=2:
            return None
        observed=Cut(tuple(body['episode']),body['duration'],tuple(tuple(a) for a in body['anchors']))
        with self.db() as db:
            identity=db.execute('SELECT linked_to FROM actors WHERE id=?',(actor,)).fetchone()
            if not identity or identity['linked_to'] is not None:
                return None
            old=db.execute('SELECT target,applied FROM catalogue_ingested WHERE receipt=?',(receipt,)).fetchone()
            if old:
                clock=db.execute('SELECT sequence FROM catalogue_clock WHERE actor=? AND target=?',(actor,old['target'])).fetchone()
                return dict(target=old['target'],applied=bool(old['applied'] and clock and clock['sequence']==sequence and self._active(db,old['target'])))
            cut_id=match_cut(observed,self._cuts(db))
            if not cut_id:
                return None
            rows=db.execute('''SELECT targets.*,catalogue_markers.public_id FROM targets
                JOIN records USING(record) JOIN catalogue_markers ON targets.id=catalogue_markers.target
                WHERE records.cut=? AND records.marker=? AND catalogue_markers.state='active' ''',
                (cut_id,body['marker'])).fetchall()
            matches=[r for r in rows if abs(r['start']-body['start'])<.01 and abs(r['end']-body['end'])<.01]
            reference=body.get('reference')
            if reference:
                matches=[r for r in matches if r['public_id']==reference['marker'] and r['revision']==reference['revision']]
            if len(matches)!=1:
                return None
            target=matches[0]['id']
            clock=db.execute('SELECT sequence FROM catalogue_clock WHERE actor=? AND target=?',(actor,target)).fetchone()
            applied=not clock or sequence>clock['sequence']
            if applied:
                self.votes._cast(db,actor,target,body['assessment']=='good')
                db.execute('INSERT OR REPLACE INTO catalogue_clock VALUES(?,?,?)',(actor,target,sequence))
            db.execute('INSERT INTO catalogue_ingested VALUES(?,?,?,?,?)',(receipt,sequence,actor,target,int(applied)))
            return dict(target=target,applied=applied)

    def ledger(self):
        with self.db() as db:
            cuts=self._cuts(db)
            proposals={cut:[] for cut in cuts}
            for r in db.execute('''SELECT targets.*,records.cut,records.marker,catalogue_markers.software,catalogue_markers.state
                    FROM targets JOIN records USING(record) JOIN catalogue_markers ON targets.id=catalogue_markers.target'''):
                proposals[r['cut']].append(Proposal(r['record'],r['revision'],r['marker'],r['start'],r['end'],bool(r['software']),r['state']=='withdrawn'))
            signed,anonymous,trusted,eligible,linked=[],[],set(),set(),set()
            for r in db.execute('''SELECT actors.*,catalogue_policy.eligible,catalogue_policy.trusted
                    FROM actors LEFT JOIN catalogue_policy ON actors.id=catalogue_policy.actor'''):
                if r['kind']=='anonymous' and r['linked_to'] is not None:
                    linked.add(r['identity'])
                if r['linked_to'] is None and r['trusted'] and r['kind']=='signed_in':
                    trusted.add(int(r['identity']))
                if r['linked_to'] is None and r['eligible'] and r['kind']=='anonymous':
                    eligible.add(r['identity'])
            for r in db.execute('''SELECT actors.kind,actors.identity,targets.record,targets.revision,votes.good
                    FROM votes JOIN actors ON votes.actor=actors.id JOIN targets ON votes.target=targets.id
                    WHERE actors.linked_to IS NULL ORDER BY votes.event'''):
                if r['kind']=='signed_in':
                    signed.append(Review(int(r['identity']),r['record'],r['revision'],bool(r['good'])))
                else:
                    anonymous.append(AnonymousReview(r['identity'],r['record'],r['revision'],bool(r['good'])))
        return cuts,proposals,signed,trusted,dict(anonymous_reviews=anonymous,eligible_keys=eligible,linked_keys=linked)
