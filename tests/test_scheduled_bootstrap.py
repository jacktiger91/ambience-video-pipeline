"""Bootstrap permission boundaries and retry reuse without remote transfers."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import scheduled_bootstrap as bootstrap
import filebin_delivery
try:  # Support both unittest discovery and explicit tests.test_* invocation.
    from . import test_encrypted_queue as queue_fixtures
except ImportError:
    import test_encrypted_queue as queue_fixtures


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

    def prepared_fixture(self):
        # Reuse the queue's intentionally fake media: these tests exercise
        # manifest, transport and retry boundaries, never claim a real render.
        fixture = queue_fixtures.EncryptedQueueTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        bundle_path, backdrop, motion = fixture.fixture()
        bundle, files, identity = filebin_delivery.build_manifest(bundle_path, backdrop)
        expected = {name: {"size": item["size"], "sha256": item["sha256"]} for name, item in files.items()}
        folder = "https://filebin.net/" + "a"*32
        receipt = {"schema_version": 1, "provider": "filebin.net", "bin": "a"*32,
                   "bundle_id": bundle["bundle_id"], "identity": identity,
                   "expected_files": expected, "status": "delivered", "readonly": True,
                   "folder_url": folder, "expires_at": "2100-11-12T18:00:00Z",
                   "files": {name: {**item, "url": folder + "/" + name, "uploaded": True,
                                    "download_verified": True,
                                    "download_verification_method": "one complete HTTP GET, SHA-256 and byte count"}
                             for name, item in expected.items()}}
        data = {"schema_version": 1, "kind": "prepared-bundle", "bundle_id": bundle["bundle_id"],
                "files": [{"name": name, "url": folder + "/" + name, **item} for name, item in expected.items()],
                "bundle": bundle, "motion_review": json.loads(motion.read_text()), "delivery_receipt": receipt}
        self.store.guard.return_value = {"body": bootstrap.START + json.dumps(data) + bootstrap.END}
        self.store.latest_receipt.return_value = (None, 0)
        self.store.publish_private_review.return_value = "https://github.com/owner/repo/releases/tag/private-draft"
        self.store.save_receipt.side_effect = lambda value: value
        def fetch(source, destination, maximum):
            name = source["url"].rsplit("/", 1)[-1]
            item = files[name]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(Path(item["path"]).read_bytes() if "path" in item else item["text"].encode())
        return data, fetch

    def test_episode_import_keeps_identity_and_retry_reuses_archive_without_filebin_gets(self):
        data, fetch = self.prepared_fixture()
        captured = {}
        def upload(store, source, name, sha):
            captured["archive"] = source.read_bytes()
            captured["asset"] = {"id": 123, "name": name, "size": source.stat().st_size, "state": "uploaded"}
            return captured["asset"]
        with mock.patch.object(bootstrap, "fetch_source", side_effect=fetch) as downloaded, \
                mock.patch.object(bootstrap, "upload_private_file", side_effect=upload):
            first = bootstrap.bootstrap_episode(self.store, self.root)
        self.assertEqual(downloaded.call_count, 8)
        self.assertEqual(first["delivery_identity"], data["delivery_receipt"]["identity"])
        self.assertNotIn("filebin.net", json.dumps(first))
        self.assertFalse(first["media_delivered"])
        retry = self.root / "retry"; retry.mkdir()
        self.store.assets.return_value = [captured["asset"]]
        self.store.latest_receipt.return_value = (data["delivery_receipt"], 1)
        self.store.download.side_effect = lambda asset, path: path.write_bytes(captured["archive"])
        with mock.patch.object(bootstrap, "fetch_source") as fetch, \
                mock.patch.object(bootstrap, "upload_private_file") as upload:
            second = bootstrap.bootstrap_episode(self.store, retry)
        self.assertTrue(second["reused_existing_asset"])
        self.assertEqual(second["sha256"], first["sha256"])
        fetch.assert_not_called(); upload.assert_not_called()

    def test_changed_copy_or_extra_source_cannot_enter_prepared_queue(self):
        data, fetch = self.prepared_fixture()
        data["bundle"]["videos"][0]["title"] = "Changed after completed delivery"
        self.store.guard.return_value = {"body": bootstrap.START + json.dumps(data) + bootstrap.END}
        with mock.patch.object(bootstrap, "fetch_source", side_effect=fetch), \
                mock.patch.object(bootstrap, "upload_private_file") as upload, \
                self.assertRaisesRegex(bootstrap.BootstrapError, "differs from the original"):
            bootstrap.bootstrap_episode(self.store, self.root)
        upload.assert_not_called()
        self.store.save_receipt.assert_not_called()
        data["files"].append({"name": "token.json", "url": "https://filebin.net/" + "a"*32 + "/token.json"})
        with self.assertRaisesRegex(bootstrap.BootstrapError, "eight-file"):
            bootstrap.episode_specification({"body": bootstrap.START + json.dumps(data) + bootstrap.END})


if __name__ == "__main__":
    unittest.main()
