"""Bootstrap permission boundaries and retry reuse without remote transfers."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import scheduled_bootstrap as bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bootstrap-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = {"url": "https://filebin.net/" + "a"*32 + "/approved-copy.txt",
                       "size": 4, "sha256": hashlib.sha256(b"copy").hexdigest()}
        self.data = {"schema_version": 1, "kind": "connection-probe", "source": self.source}
        self.release = {"draft": True, "body": "private notes\n" + bootstrap.START + "\n" + json.dumps(self.data) + "\n" + bootstrap.END}
        self.store = mock.Mock()
        self.store.guard.return_value = self.release
        self.store.assets.return_value = []

    def test_private_spec_and_new_probe_do_not_expose_url_or_copy(self):
        asset = {"id": 77, "name": bootstrap.PROBE_NAME, "size": 4, "state": "uploaded"}
        with mock.patch.object(bootstrap, "fetch_source") as fetch, \
                mock.patch.object(bootstrap, "upload_private_file", return_value=asset) as upload:
            result = bootstrap.bootstrap_probe(self.store, self.root)
        fetch.assert_called_once()
        upload.assert_called_once()
        self.assertEqual(result["asset_id"], 77)
        self.assertNotIn("url", result)
        self.assertNotIn("filebin.net", json.dumps(result))
        self.assertNotIn("private notes", json.dumps(result))

    def test_retry_verifies_existing_private_asset_without_source_download_or_upload(self):
        self.store.assets.return_value = [{"id": 77, "name": bootstrap.PROBE_NAME, "size": 4, "state": "uploaded"}]
        with mock.patch.object(bootstrap, "fetch_source") as fetch, \
                mock.patch.object(bootstrap, "upload_private_file") as upload:
            result = bootstrap.bootstrap_probe(self.store, self.root)
        self.assertTrue(result["reused_existing_asset"])
        fetch.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(self.store.download.call_args.args[2:], (self.source["sha256"], 4))

    def test_only_explicit_bounded_filebin_text_is_eligible(self):
        for change in ({"url": "https://example.invalid/file.txt"}, {"size": 200000}, {"sha256": "bad"}, {"token": "unit-fixture"}):
            data = {**self.source, **change}
            with self.subTest(change=change), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.check_source(data, 128*1024)
        malformed = {"body": bootstrap.START + '{"schema_version":1,"kind":"episode"}' + bootstrap.END}
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.specification(malformed, "connection-probe")

    def test_complete_source_bytes_are_checked_before_accepting_download(self):
        response = mock.MagicMock()
        response.status = 200
        response.headers.get_content_type.return_value = "text/plain"
        response.__enter__.return_value = response
        response.read.side_effect = [b"copy", b""]
        client = mock.Mock()
        client.open.return_value = response
        with mock.patch("scheduled_bootstrap.urllib.request.build_opener", return_value=client):
            bootstrap.fetch_source(self.source, self.root / "okay.txt", 128*1024)
        self.assertEqual((self.root / "okay.txt").read_bytes(), b"copy")
        response.read.side_effect = [b"nope", b""]
        with mock.patch("scheduled_bootstrap.urllib.request.build_opener", return_value=client), \
                self.assertRaisesRegex(bootstrap.BootstrapError, "SHA-256"):
            bootstrap.fetch_source(self.source, self.root / "bad.txt", 128*1024)
        self.assertFalse((self.root / "bad.txt").exists())


if __name__ == "__main__":
    unittest.main()
