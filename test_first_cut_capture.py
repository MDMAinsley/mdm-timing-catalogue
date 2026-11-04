import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import kodi_live
import live_catalogue


class FirstCutTests(unittest.TestCase):
    def test_explicit_private_draft_can_capture_first_cut_but_empty_normal_lookup_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            catalogue=root/'empty.json'
            catalogue.write_text(json.dumps(dict(schema=2,cuts={})))
            args=['kodi_live.py','--fpcalc',str(root/'fpcalc'),'--output',str(root),
                  '--catalogue',str(catalogue),'--bridge']
            item=dict(tmdb_id=2316,season=4,episode=10)
            with (patch('sys.argv',args),patch.object(kodi_live,'capture') as capture,
                  contextlib.redirect_stdout(io.StringIO())):
                kodi_live.main()
                capture.assert_not_called()
            with (patch('sys.argv',args+['--save-cut',str(root/'draft.json')]),
                  patch.object(kodi_live,'capture',return_value=(item,Mock(),{})) as capture,
                  patch.object(live_catalogue,'apply_capture',return_value={}),
                  patch.object(live_catalogue,'save_cut_draft') as save,
                  contextlib.redirect_stdout(io.StringIO())):
                kodi_live.main()
                self.assertEqual(capture.call_args.args[3],30)
                self.assertIsNone(capture.call_args.kwargs['catalogue_episodes'])
                save.assert_called_once()


if __name__=='__main__':
    unittest.main()
