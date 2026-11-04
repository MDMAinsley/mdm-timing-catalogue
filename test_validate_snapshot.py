import json
from pathlib import Path
import tempfile
import unittest

from catalogue import Cut, Proposal
from export_catalogue import compile_snapshot
from live_catalogue import METHOD
from validate_snapshot import validate


class SnapshotValidationTests(unittest.TestCase):
    def test_exported_payload_passes_and_personal_fields_are_rejected(self):
        cut = Cut((123,1,1),1300,((METHOD,0,'a'*64),(METHOD,16,'b'*64)))
        snapshot = compile_snapshot({'cut':cut},
            {'cut':[Proposal('private',1,'intro',94,126,True)]},[],set())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'snapshot.json'
            path.write_text(json.dumps(snapshot),encoding='utf8')
            self.assertEqual(validate(path),(1,1))
            next(iter(snapshot['cuts'].values()))['device']='private'
            path.write_text(json.dumps(snapshot),encoding='utf8')
            with self.assertRaises(ValueError):
                validate(path)

    def test_revision_must_be_a_positive_integer(self):
        cut = Cut((123,1,1),1300,((METHOD,0,'a'*64),(METHOD,16,'b'*64)))
        snapshot = compile_snapshot({'cut':cut},
            {'cut':[Proposal('private',1,'intro',94,126,True)]},[],set())
        marker = next(iter(snapshot['cuts'].values()))['markers']['intro']
        marker['revision']=True
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'snapshot.json'
            path.write_text(json.dumps(snapshot),encoding='utf8')
            with self.assertRaises(ValueError):
                validate(path)
