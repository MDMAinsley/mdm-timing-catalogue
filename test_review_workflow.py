import io
import json
from pathlib import Path
import tempfile
import unittest

from export_catalogue import compile_snapshot, cut_identifier
from review_workflow import bundle, validate_bundle, report, process, correction_candidates
from submission_api import App, digest


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)/'private'
        self.app=App(self.directory)
        self.registry=self.app.registry
        self.body=dict(episode=[2316,4,10],duration=1323,
            anchors=[['chromaprint2-fixed-32-v1',0,'a'*64],['chromaprint2-fixed-32-v1',16,'b'*64]],
            marker='intro',start=94,end=126,origin='manual')
        self.token=self.register()

    def call(self,path,body,token=None):
        raw=json.dumps(body).encode()
        env=dict(PATH_INFO=path,REQUEST_METHOD='POST',CONTENT_TYPE='application/json',
                 CONTENT_LENGTH=str(len(raw)),**{'wsgi.input':io.BytesIO(raw)})
        if token:
            env['HTTP_AUTHORIZATION']='Bearer '+token
        status=[]
        result=b''.join(self.app(env,lambda s,h:status.append(int(s.split()[0]))))
        return status[0],json.loads(result)

    def register(self):
        return self.call('/v1/contributors',dict(consent=True))[1]['token']

    def admit(self,body=None,software=False):
        return self.registry.admit(body or self.body,confirmed_cut=True,software=software,operator='test-admin')

    def send(self,index,assessment='good',body=None,token=None):
        payload=dict(body or self.body,operation=format(index,'032x'),assessment=assessment)
        status,result=self.call('/v1/proposals',payload,token or self.token)
        self.assertEqual(status,202,result)
        return result

    def snapshot(self):
        cuts,proposals,reviews,trusted,options=self.registry.ledger()
        return compile_snapshot(cuts,proposals,reviews,trusted,**options)

    def approve(self,target):
        owner=self.app.votes.signed_in(1234567)
        self.registry.policy(owner,trusted=True,operator='test-admin')
        self.registry.review(target,owner,True,operator='test-admin')
        return owner

    def test_untrusted_inbox_cannot_create_cut_or_trusted_approval(self):
        self.assertFalse(self.send(1)['counted_vote'])
        self.assertEqual(self.snapshot(),dict(schema=2,cuts={}))
        for name in ('admit','review','policy','withdraw'):
            self.assertEqual(self.call('/v1/'+name,{},self.token)[0],404)
        guest=self.app.votes.anonymous(digest(self.token))
        with self.assertRaises(ValueError):
            self.registry.policy(guest,trusted=True,operator='test-admin')
        with self.assertRaises(ValueError):
            self.registry.admit(self.body,operator='test-admin')

    def test_cut_and_marker_ids_normalize_integer_float_runtime(self):
        first=self.admit()
        self.assertEqual(first,self.admit(dict(self.body,duration=1323.0)))
        self.approve(first)
        snapshot=self.snapshot()
        cuts=self.registry.ledger()[0]
        cut=next(iter(cuts.values()))
        self.assertEqual(next(iter(snapshot['cuts'])),cut_identifier(cut))
        self.assertEqual(len(snapshot['cuts']),1)

    def test_exact_cut_vote_recorded_new_keys_cannot_promote(self):
        target=self.admit()
        self.assertTrue(self.send(1)['counted_vote'])
        self.assertEqual(self.app.votes.totals(target)['anonymous']['good'],1)
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers'],{})

    def test_wrong_cut_runtime_reference_and_missing_evidence_never_attach(self):
        self.admit()
        bodies=[dict(self.body,anchors=[]),dict(self.body,duration=1400),
                dict(self.body,anchors=[self.body['anchors'][0],['chromaprint2-fixed-32-v1',16,'f'*64]]),
                dict(self.body,reference=dict(marker='c'*64,revision=1)),dict(self.body,start=95)]
        for index,body in enumerate(bodies,1):
            self.assertFalse(self.send(index,body=body)['counted_vote'])

    def test_retry_and_reversed_backfill_do_not_overwrite_latest_vote(self):
        target=self.admit()
        first=self.send(1,'good')
        self.send(2,'bad')
        self.assertFalse(self.send(1,'good')['counted_vote'])
        self.send(3,'good')
        self.assertEqual(self.app.votes.totals(target)['anonymous'],
                         dict(good=1,bad=0,total=1,positive_percent=100.0))
        process(self.directory)
        self.assertEqual(self.app.votes.totals(target)['anonymous']['good'],1)
        status,result=self.call('/v1/proposals',dict(self.body,operation=format(1,'032x'),assessment='bad'),self.token)
        self.assertEqual(status,400)

    def test_unmatched_receipts_backfill_once_after_verified_cut_admission(self):
        self.send(1,'good')
        self.send(2,'bad')
        target=self.admit()
        process(self.directory)
        process(self.directory)
        self.assertEqual(self.app.votes.totals(target)['anonymous']['bad'],1)
        with self.registry.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],2)

    def test_legacy_and_corrections_remain_pending(self):
        target=self.admit()
        self.assertFalse(self.call('/v1/proposals',dict(self.body,assessment='good'),self.token)[1]['counted_vote'])
        self.assertFalse(self.send(1,'correction')['counted_vote'])
        self.assertEqual(self.app.votes.totals(target)['anonymous']['total'],0)

    def test_new_revision_has_fresh_votes_and_old_or_withdrawn_targets_rejected(self):
        first=self.admit()
        self.approve(first)
        self.send(1)
        second=self.admit(dict(self.body,end=128))
        self.assertEqual(self.app.votes.totals(second)['anonymous']['total'],0)
        self.assertFalse(self.send(2)['counted_vote'])
        self.assertEqual(self.call('/v1/votes',dict(target=first,good=True),self.token)[0],404)
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers'],{})
        self.approve(second)
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers']['intro']['end'],128)
        self.registry.withdraw(second,operator='test-admin')
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers'],{})

    def test_ten_eligible_bad_votes_make_one_public_review_entry(self):
        target=self.admit(software=True)
        for index in range(10):
            token=self.register()
            actor=self.app.votes.anonymous(digest(token))
            self.registry.policy(actor,eligible=True,operator='test-admin')
            self.send(index+1,'bad',token=token)
        path=Path(self.temp.name)/'public-release'
        bundle(self.directory,path)
        metadata=validate_bundle(path)
        self.assertEqual(metadata['review_entries'],1)
        # Ten eligible Bad flags review; anonymous promotion/removal quorum is twenty.
        self.assertEqual(metadata['markers'],1)
        raw=''.join(p.read_text() for p in path.iterdir())
        self.assertNotIn(self.token,raw)
        self.assertNotIn('contributor_hash',raw)
        self.assertNotIn('identity',raw)
        with self.assertRaises(FileExistsError):
            bundle(self.directory,path)

    def test_trusted_veto_and_role_removal_change_export_without_changing_history(self):
        target=self.admit(software=True)
        owner=self.approve(target)
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers']['intro']['tier'],'trusted reviewer')
        self.registry.review(target,owner,False,operator='test-admin')
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers'],{})
        self.registry.policy(owner,trusted=False,operator='test-admin')
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers']['intro']['tier'],'software generated')

    def test_public_bundle_rejects_private_fields_even_with_rehashed_manifest(self):
        target=self.admit()
        self.approve(target)
        path=Path(self.temp.name)/'public-release'
        bundle(self.directory,path)
        p=path/'playback-snapshot.json'
        body=json.loads(p.read_bytes())
        next(iter(body['cuts'].values()))['device']='private'
        p.write_text(json.dumps(body))
        import hashlib
        manifest=json.loads((path/'catalogue-release.json').read_bytes())
        manifest['files']['playback-snapshot.json']=hashlib.sha256(p.read_bytes()).hexdigest()
        (path/'catalogue-release.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            validate_bundle(path)

    def test_signed_correction_cluster_rejects_outlier_and_only_suggests_review(self):
        self.admit()
        for index in range(6):
            token=self.register()
            guest=self.app.votes.anonymous(digest(token))
            signed=self.app.votes.signed_in(index+100)
            self.app.votes.link(guest,signed)
            # Identity fixture inserts a verified signed-in submission directly;
            # there is deliberately no public identity-claim HTTP route.
            body=dict(self.body,operation=format(index+1,'032x'),assessment='correction',
                      start=94 if index<5 else 47,end=126+index%2 if index<5 else 77)
            with self.app.db() as db:
                receipt=format(index+1,'064x')
                db.execute('INSERT INTO inbox VALUES(?,?,?,?,?)',(receipt,signed,json.dumps(body),index,'pending_review'))
                db.execute('INSERT INTO ingestion(receipt) VALUES(?)',(receipt,))
        suggestions=correction_candidates(self.directory)
        self.assertEqual(len(suggestions),1)
        self.assertEqual(suggestions[0]['start'],94)
        self.assertEqual(suggestions[0]['end'],126)
        self.assertEqual(suggestions[0]['state'],'requires_review')
        self.assertEqual(next(iter(self.snapshot()['cuts'].values()))['markers'],{})


if __name__=='__main__':
    unittest.main()
