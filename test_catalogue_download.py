import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from catalogue_download import URL, refresh


class Response(io.BytesIO):
    def geturl(self):
        return URL


class CatalogueDownloadTests(unittest.TestCase):
    def test_refresh_reuse_and_preserve_after_invalid_or_offline_response(self):
        payload = b'{"schema":2,"cuts":{}}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public-catalogue-cache.json'
            with patch('catalogue_download.urlopen', return_value=Response(payload)) as fetch:
                self.assertEqual(refresh(path), ({}, {}))
                self.assertEqual(refresh(path), ({}, {}))
                fetch.assert_called_once()
            future = time.time() + 90000
            with patch('catalogue_download.urlopen', return_value=Response(b'{"schema":2,"cuts":{"invalid":{}}}')):
                self.assertEqual(refresh(path, now=future), ({}, {}))
            self.assertEqual(path.read_bytes(), payload)
            with patch('catalogue_download.urlopen', side_effect=OSError):
                self.assertEqual(refresh(path, now=future), ({}, {}))
            self.assertEqual(path.read_bytes(), payload)

    def test_first_failure_is_safe_and_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public-catalogue-cache.json'
            with patch('catalogue_download.urlopen', side_effect=OSError) as fetch:
                self.assertIsNone(refresh(path))
                self.assertIsNone(refresh(path))
                fetch.assert_called_once()
                self.assertFalse(path.exists())
