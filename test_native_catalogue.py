import json
from pathlib import Path
import tempfile
import unittest

from cut_evidence import AUDIO,RANGES,normalize
from modern_catalogue import read,matching
from review_workflow import bundle,validate_bundle
from submission_api import App,validate_proposal


class NativeCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.app=App(self.root/'private')
        self.r=self.app.registry
        self.audio=[[AUDIO,0,'a'*64],[AUDIO,16,'b'*64]]
        self.native=[[RANGES,slot,char*64] for slot,char in zip((0,25,75,100),'cdef')]
        self.body=dict(episode=[2316,4,10],duration=1313,anchors=self.audio,marker='intro',start=96,end=128,origin='manual')

    def approve(self,target):
        owner=self.r.votes.signed_in(123)
        self.r.policy(owner,trusted=True,operator='test-admin')
        self.r.review(target,owner,True,operator='test-admin')

    def test_alias_keeps_original_cut_vote_and_revision_and_old_snapshot_valid(self):
        target=self.r.admit(self.body,confirmed_cut=True,operator='test-admin')
        self.approve(target)
        cut=next(iter(self.r.ledger()[0]))
        with self.assertRaises(ValueError):
            self.r.add_evidence(cut,self.native,operator='test-admin')
        self.r.add_evidence(cut,self.native,confirmed_file=True,operator='test-admin')
        path=self.root/'release'
        bundle(self.r.directory,path)
        validate_bundle(path)
        legacy=json.loads((path/'playback-snapshot.json').read_bytes())
        modern=read(path/'playback-snapshot-v3.json')
        self.assertEqual(legacy['cuts'][cut]['anchors'],self.audio)
        self.assertEqual(matching(modern,self.body['episode'],1313,self.native),[cut])
        self.assertEqual(matching(modern,self.body['episode'],1313,self.audio),[cut])
        self.assertEqual(legacy['cuts'][cut]['markers'],modern['cuts'][cut]['markers'])
        byte_body=dict(self.body,anchors=self.native)
        self.assertEqual(self.r.admit(byte_body,confirmed_cut=True,operator='test-admin'),target)
        guest=self.r.votes.anonymous('f'*64)
        result=self.r.apply(guest,'e'*64,1,dict(byte_body,operation='d'*32,assessment='good'))
        self.assertEqual(result['target'],target)
        self.assertEqual(self.r.votes.totals(target)['anonymous']['good'],1)

    def test_byte_only_cut_is_not_sent_to_old_clients(self):
        body=dict(self.body,anchors=self.native)
        validate_proposal(body)
        target=self.r.admit(body,confirmed_cut=True,operator='test-admin')
        self.approve(target)
        path=self.root/'release'
        bundle(self.r.directory,path)
        metadata=validate_bundle(path)
        self.assertEqual(metadata['cuts'],1)
        self.assertEqual(json.loads((path/'playback-snapshot.json').read_bytes())['cuts'],{})
        modern=read(path/'playback-snapshot-v3.json')
        cut=next(iter(modern['cuts']))
        self.assertEqual(matching(modern,self.body['episode'],1313,self.native),[cut])
        bad=[row[:] for row in self.native]
        bad[-1][-1]='0'*64
        self.assertEqual(matching(modern,self.body['episode'],1313,bad),[])

    def test_alias_cannot_steal_matching_evidence_from_another_cut(self):
        first=self.r.admit(self.body,confirmed_cut=True,operator='test-admin')
        first_cut=next(iter(self.r.ledger()[0]))
        self.r.admit(dict(self.body,anchors=self.native),confirmed_cut=True,operator='test-admin')
        with self.assertRaises(ValueError):
            self.r.add_evidence(first_cut,self.native,confirmed_file=True,operator='test-admin')

    def test_complete_method_specific_evidence_only(self):
        for group in (self.native[:2],self.audio+self.native[:2],[[RANGES,True,'a'*64]]*4):
            with self.assertRaises(ValueError):normalize(group)

    def test_existing_audio_only_public_bundle_remains_verifiable(self):
        target=self.r.admit(self.body,confirmed_cut=True,operator='test-admin')
        self.approve(target)
        path=self.root/'release'
        bundle(self.r.directory,path)
        (path/'playback-snapshot-v3.json').unlink()
        manifest=json.loads((path/'catalogue-release.json').read_bytes())
        del manifest['files']['playback-snapshot-v3.json']
        (path/'catalogue-release.json').write_text(json.dumps(manifest))
        self.assertEqual(validate_bundle(path)['cuts'],1)

    def test_audio_alias_can_project_byte_primary_cut_to_legacy_without_new_target(self):
        target=self.r.admit(dict(self.body,anchors=self.native),confirmed_cut=True,operator='test-admin')
        self.approve(target)
        cut=next(iter(self.r.ledger()[0]))
        self.r.add_evidence(cut,self.audio,confirmed_file=True,operator='test-admin')
        path=self.root/'release'
        bundle(self.r.directory,path)
        validate_bundle(path)
        old=json.loads((path/'playback-snapshot.json').read_bytes())
        self.assertEqual(old['cuts'][cut]['anchors'],self.audio)
        self.assertEqual(old['cuts'][cut]['markers']['intro']['tier'],'trusted reviewer')


if __name__=='__main__':
    unittest.main()
