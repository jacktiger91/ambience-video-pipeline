"""Private queue access and daily delivery decisions with no network writes."""
from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.request

import scheduled_runner as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runner-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.json"
        self.config = {"schema_version": 1, "enabled": False, "timezone": "Asia/Nicosia",
                       "attempt_from": "16:00", "ready_by": "18:00", "first_delivery_date": "2026-11-05",
                       "concept_catalog": "concepts.json", "repository": "owner/project",
                       "private_release_id": 77, "private_release_tag": "private-review",
                       "draft_privacy_verified": False, "prepared_episodes": {}}
        (self.root / "concepts.json").write_text(json.dumps({"rotation_start_date": "2026-11-04", "concepts": [
            {"id": "shore"}, {"id": "forest"}]}))
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.config))

    def slot(self):
        return {"episode_date": "2026-11-05", "ready_by": "2026-11-05T18:00:00+02:00",
                "ready_by_utc": "2026-11-05T16:00:00+00:00", "deadline_passed": False,
                "enabled": True, "window_open": True, "blockers": [], "bundle_id": "2026-11-05-forest"}

    def completed(self):
        files = {f"2026-11-05-forest-{name}{suffix}": {"size": index + 20, "sha256": "b"*64}
                 for index, (name, suffix) in enumerate([
                     ("long", ".mp4"), ("short1", ".mp4"), ("short2", ".mp4"),
                     ("thumbnail", ".jpg"), ("short1-cover", ".jpg"), ("short2-cover", ".jpg"),
                     ("backdrop", ".jpg"), ("upload-details", ".txt")])}
        receipt = {"identity": "a"*64, "status": "delivered", "expires_at": "2100-11-12T18:00:00Z",
                   "bin": "b"*32, "expected_files": files}
        listing = {"bin": {"readonly": True, "expired_at": receipt["expires_at"]},
                   "files": [{"filename": name, "bytes": item["size"], "sha256": item["sha256"]} for name, item in files.items()]}
        return receipt, listing

    def test_disabled_schedule_performs_no_network_or_store_access(self):
        with mock.patch.object(runner, "DraftStore") as store:
            result = runner.run(self.path, self.root / "work", "scheduled")
        self.assertEqual(result["status"], "not_due_or_disabled")
        store.assert_not_called()

    def test_release_must_be_exact_private_draft(self):
        store = runner.DraftStore(self.config, "unit-fixture-token")
        release = {"id": 77, "tag_name": "private-review", "draft": False}
        with mock.patch.object(store.api, "request", return_value=(200, release)):
            with self.assertRaisesRegex(runner.RunnerError, "expected private draft"):
                store.guard()
            release["draft"] = True
            self.assertIs(store.guard(), release)
            release["tag_name"] = "another-draft"
            with self.assertRaises(runner.RunnerError):
                store.guard()

    def test_redirect_never_sends_github_authorization_to_asset_storage(self):
        request = urllib.request.Request("https://api.github.com/repos/owner/project/releases/assets/5",
                                         headers={"Authorization": "Bearer unit-fixture-token"})
        redirect = runner.SafeRedirect().redirect_request(request, None, 302, "Found", {},
                                                          "https://release-assets.githubusercontent.com/private-object")
        self.assertIsNone(redirect.get_header("Authorization"))
        with self.assertRaisesRegex(runner.RunnerError, "HTTPS"):
            runner.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "http://example.invalid/file")

    def test_transient_anonymous_failure_retries_but_still_requires_actual_404(self):
        import urllib.error
        store=runner.DraftStore(self.config,'unit-fixture-token')
        limited=urllib.error.HTTPError('https://example.invalid',403,'rate limit',{'X-RateLimit-Remaining':'0'},None)
        hidden=urllib.error.HTTPError('https://example.invalid',404,'Not Found',{},None)
        with mock.patch.object(store,'guard'),mock.patch.object(runner.urllib.request,'urlopen',side_effect=[limited,hidden]) as open_url, \
                mock.patch.object(runner.time,'sleep') as pause:
            store.anonymous_probe()
            self.assertEqual(open_url.call_count,2);pause.assert_called_once_with(5)
        forbidden=urllib.error.HTTPError('https://example.invalid',403,'Forbidden',{},None)
        with mock.patch.object(store,'guard'),mock.patch.object(runner.urllib.request,'urlopen',side_effect=forbidden):
            with self.assertRaisesRegex(runner.RunnerError,'HTTP 403'):
                store.anonymous_probe()

    def test_completed_receipt_skips_media_download_and_has_no_public_filebin_url(self):
        self.config.update({"enabled": True, "draft_privacy_verified": True})
        self.config["prepared_episodes"]["2026-11-05"] = {"bundle_id": "2026-11-05-forest", "delivery_identity": "a"*64}
        self.write()
        store = mock.Mock()
        receipt, listing = self.completed()
        store.latest_receipt.return_value = (receipt, 8)
        store.publish_private_review.return_value = "https://github.com/owner/project/releases/tag/private-review"
        with mock.patch.object(runner.daily_schedule, "select_slot", return_value=self.slot()), \
                mock.patch.object(runner, "DraftStore", return_value=store), \
                mock.patch.object(runner.filebin_delivery, "FilebinClient") as client, \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "unit-fixture-token"}):
            client.return_value.listing.return_value = listing
            result = runner.run(self.path, self.root / "work", "scheduled")
        self.assertEqual(result["status"], "already_delivered")
        store.find_asset.assert_not_called()
        store.download.assert_not_called()
        client.return_value.listing.assert_called_once_with(receipt["bin"])
        client.return_value.verify_download.assert_not_called()
        client.return_value.upload.assert_not_called()
        self.assertTrue(result["filebin_metadata_verified"])
        self.assertNotIn("filebin.net", json.dumps(result))

    def test_new_dates_resolve_their_own_private_queue_and_reuse_delivery(self):
        import production_store
        self.config.update({'enabled':True,'draft_privacy_verified':True,'automatic_production_start':'2026-11-05'})
        self.write()
        legacy=mock.Mock();dated=mock.Mock();receipt,listing=self.completed()
        dated.latest_receipt.return_value=(receipt,8)
        dated.publish_private_review.return_value='https://github.com/owner/project/releases'
        entry={'bundle_id':'2026-11-05-forest','delivery_identity':'a'*64}
        with mock.patch.object(runner.daily_schedule,'select_slot',return_value=self.slot()), \
                mock.patch.object(runner,'DraftStore',return_value=legacy), \
                mock.patch.object(production_store,'daily_store',return_value=dated), \
                mock.patch.object(production_store,'prepared_entry',return_value=entry), \
                mock.patch.object(runner.filebin_delivery,'FilebinClient') as client, \
                mock.patch.dict(os.environ,{'GITHUB_TOKEN':'unit-fixture-token'}):
            client.return_value.listing.return_value=listing
            result=runner.run(self.path,self.root/'work','scheduled')
        self.assertEqual(result['status'],'already_delivered')
        legacy.latest_receipt.assert_not_called()
        dated.latest_receipt.assert_called_once_with('2026-11-05-forest')
        dated.download.assert_not_called()
        client.return_value.upload.assert_not_called()

    def test_expired_complete_receipt_is_not_reported_as_ready(self):
        self.config.update({"enabled": True, "draft_privacy_verified": True})
        self.config["prepared_episodes"]["2026-11-05"] = {"bundle_id": "2026-11-05-forest", "delivery_identity": "a"*64}
        self.write()
        store = mock.Mock()
        store.latest_receipt.return_value = ({"identity": "a"*64, "status": "delivered", "expires_at": "2000-11-12T18:00:00Z"}, 8)
        with mock.patch.object(runner.daily_schedule, "select_slot", return_value=self.slot()), \
                mock.patch.object(runner, "DraftStore", return_value=store), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "unit-fixture-token"}):
            with self.assertRaisesRegex(runner.RunnerError, "expires before"):
                runner.run(self.path, self.root / "work", "scheduled")
        store.publish_private_review.assert_not_called()

    def test_manual_delivery_requires_date_and_reuses_receipt_with_disabled_schedule(self):
        with self.assertRaisesRegex(runner.RunnerError, "explicit approved episode date"):
            runner.run(self.path, self.root / "work", "delivery-test")
        self.config["prepared_episodes"]["2026-11-05"] = {"bundle_id": "2026-11-05-forest", "delivery_identity": "a"*64}
        self.write()
        store = mock.Mock()
        receipt, listing = self.completed()
        store.latest_receipt.return_value = (receipt, 8)
        store.publish_private_review.return_value = "https://github.com/owner/project/releases/tag/private-review"
        with mock.patch.object(runner, "DraftStore", return_value=store), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "unit-fixture-token"}), \
                mock.patch.object(runner.filebin_delivery, "FilebinClient") as client, \
                mock.patch.object(runner.filebin_delivery, "deliver") as deliver:
            client.return_value.listing.return_value = listing
            result = runner.run(self.path, self.root / "work", "delivery-test", "2026-11-05")
        self.assertEqual(result["status"], "already_delivered")
        store.anonymous_probe.assert_called_once()
        store.publish_private_review.assert_called_once()
        store.download.assert_not_called()
        deliver.assert_not_called()

    def test_live_listing_rejects_missing_mismatched_extra_or_unlocked_package(self):
        receipt, listing = self.completed()
        variants = []
        missing = copy.deepcopy(listing); missing["files"].pop(); variants.append(missing)
        changed = copy.deepcopy(listing); changed["files"][0]["sha256"] = "c"*64; variants.append(changed)
        wrong_size = copy.deepcopy(listing); wrong_size["files"][0]["bytes"] += 1; variants.append(wrong_size)
        duplicate = copy.deepcopy(listing); duplicate["files"][-1] = duplicate["files"][0]; variants.append(duplicate)
        extra = copy.deepcopy(listing); extra["files"].append(dict(extra["files"][0])); variants.append(extra)
        unlocked = copy.deepcopy(listing); unlocked["bin"]["readonly"] = False; variants.append(unlocked)
        deadline = datetime(2099, 11, 5, 16, tzinfo=timezone.utc)
        for value in variants:
            with self.subTest(value=value), \
                    mock.patch.object(runner.filebin_delivery, "FilebinClient") as client, \
                    self.assertRaises(runner.RunnerError):
                client.return_value.listing.return_value = value
                runner.confirm_completed_delivery(receipt, deadline)
            client.return_value.listing.assert_called_once()
            client.return_value.upload.assert_not_called()
            client.return_value.verify_download.assert_not_called()

    def test_server_expiry_must_outlast_deadline_and_current_expiry_is_retained(self):
        receipt, listing = self.completed()
        deadline = datetime(2099, 11, 5, 16, tzinfo=timezone.utc)
        with mock.patch.object(runner.filebin_delivery, "FilebinClient") as client:
            client.return_value.listing.return_value = listing
            for expiry in ("2000-11-05T16:00:00Z", "2099-11-05T16:00:00Z", "invalid", None):
                listing["bin"]["expired_at"] = expiry
                with self.subTest(expiry=expiry), self.assertRaises(runner.RunnerError):
                    runner.confirm_completed_delivery(receipt, deadline)
            listing["bin"]["expired_at"] = "2099-11-12T18:00:00Z"
            confirmed = runner.confirm_completed_delivery(receipt, deadline)
        self.assertEqual(confirmed["expires_at"], "2099-11-12T18:00:00Z")
        self.assertEqual(receipt["expires_at"], "2100-11-12T18:00:00Z")

    def test_preflight_restores_future_package_without_filebin_upload(self):
        self.config["prepared_episodes"]["2026-11-05"] = {
            "bundle_id": "2026-11-05-forest", "delivery_identity": "a"*64,
            "asset_id": 9, "asset_size": 42, "asset_sha256": "b"*64}
        self.write()
        store = mock.Mock()
        store.latest_receipt.return_value = ({"identity": "a"*64, "status": "delivered", "expires_at": "2100-11-12T18:00:00Z"}, 8)
        restored = {"delivery_identity": "a"*64, "bundle_path": self.root / "bundle.json",
                    "backdrop_path": self.root / "backdrop.jpg", "motion_report_path": self.root / "motion.json"}
        with mock.patch.object(runner, "DraftStore", return_value=store), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "unit-fixture-token"}), \
                mock.patch.object(runner, "restore_private_bundle", return_value=restored), \
                mock.patch.object(runner.filebin_delivery, "build_manifest", return_value=({}, {}, "a"*64)), \
                mock.patch.object(runner.daily_schedule, "check_motion_review"), \
                mock.patch.object(runner.filebin_delivery, "deliver") as deliver:
            result = runner.run(self.path, self.root / "work", "preflight", "2026-11-05")
        self.assertEqual(result["status"], "preflight_passed")
        self.assertFalse(result["media_delivered"])
        store.anonymous_probe.assert_called_once()
        deliver.assert_not_called()

    def test_scheduled_mode_cannot_override_date_or_skip_privacy_preflight(self):
        with self.assertRaisesRegex(runner.RunnerError, "different date"):
            runner.run(self.path, self.root / "work", "scheduled", "2026-11-05")
        with mock.patch.object(runner.daily_schedule, "select_slot", return_value=self.slot()), \
                mock.patch.object(runner, "DraftStore"), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "unit-fixture-token"}):
            with self.assertRaisesRegex(runner.RunnerError, "private-draft preflight"):
                runner.run(self.path, self.root / "work", "scheduled")


if __name__ == "__main__":
    unittest.main()
