import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from export_worker import export
import publish_catalogue as publisher
from review_workflow import validate_bundle
from submission_api import App


@unittest.skipUnless(shutil.which('git'),'Git required')
class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.data=self.root/'private'
        App(self.data)
        self.exports=self.root/'exports'
        export(self.data,self.exports)
        self.release=self.exports/(self.exports/'current-export').read_text().strip()
        self.remote=self.root/'remote.git'
        publisher.git(None,'init','--bare','--initial-branch=main',str(self.remote))
        seed=self.root/'seed'
        publisher.git(None,'clone',str(self.remote),str(seed))
        publisher.git(seed,'config','user.name','Test owner')
        publisher.git(seed,'config','user.email','test@example.invalid')
        (seed/'README.md').write_text('Keep this source file unchanged.\n')
        publisher.git(seed,'add','README.md')
        publisher.git(seed,'commit','-m','Initial test source')
        publisher.git(seed,'push','origin','main')
        self.checkout=self.root/'publisher'
        patcher=patch.object(publisher,'REMOTE',str(self.remote))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_public_files_committed_and_repeat_export_is_noop(self):
        first=publisher.publish(self.release,self.checkout)
        self.assertTrue(first['changed'])
        self.assertEqual(publisher.git(self.checkout,'show','HEAD:README.md'),'Keep this source file unchanged.')
        changed=publisher.git(self.checkout,'diff','--name-only','HEAD^','HEAD').splitlines()
        self.assertEqual(set(changed),set(publisher.PUBLIC))
        second=publisher.publish(self.release,self.checkout)
        self.assertFalse(second['changed'])
        self.assertEqual(first['commit'],second['commit'])
        self.assertEqual(publisher.git(self.checkout,'status','--porcelain'),'')

    def test_tampered_bundle_stops_before_clone(self):
        (self.release/'playback-snapshot.json').write_text('{"schema":2,"cuts":{},"secret":"private"}')
        with self.assertRaises(ValueError):
            publisher.publish(self.release,self.checkout)
        self.assertFalse(self.checkout.exists())

    def test_dirty_checkout_and_divergence_preserve_commits(self):
        publisher.publish(self.release,self.checkout)
        (self.checkout/'README.md').write_text('uncommitted change')
        with self.assertRaises(RuntimeError):
            publisher.publish(self.release,self.checkout)
        publisher.git(self.checkout,'add','README.md')
        publisher.git(self.checkout,'commit','-m','Unpublished local test change')
        local=publisher.git(self.checkout,'rev-parse','HEAD')
        seed=self.root/'seed'
        publisher.git(seed,'pull','--ff-only','origin','main')
        (seed/'other.txt').write_text('remote update')
        publisher.git(seed,'add','other.txt')
        publisher.git(seed,'commit','-m','Concurrent owner update')
        publisher.git(seed,'push','origin','main')
        with self.assertRaises(RuntimeError):
            publisher.publish(self.release,self.checkout)
        self.assertEqual(publisher.git(self.checkout,'rev-parse','HEAD'),local)

    def test_export_pointer_and_private_report_are_separate(self):
        validate_bundle(self.release)
        self.assertTrue((self.data/'review-report.json').exists())
        self.assertEqual({p.name for p in self.release.iterdir()},set(publisher.PUBLIC))
        self.assertNotIn('pending',(self.release/'catalogue-release.json').read_text())


if __name__=='__main__':
    unittest.main()
