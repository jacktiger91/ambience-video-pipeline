"""Offline contract tests: no credentials, paid services or real uploads."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import youtube
import cloud_ledger


VIDEO_ID = "abcdefghijk"
CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=PRIVATE_SESSION"


class FakeClient:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.requests = []
        self.api_calls = []
        self.owner = CHANNEL_ID
        self.privacy = "unlisted"

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        if not self.replies:
            raise AssertionError("Unexpected request")
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def api(self, method, resource, params, body=None):
        self.api_calls.append((method, resource, params, body))
        if resource == "channels":
            return {"items": [{"id": self.owner, "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]}
        if resource == "playlistItems":
            return {"items": []}
        if resource == "videos":
            return {"items": [{"id": VIDEO_ID, "snippet": {"channelId": self.owner, "tags": []},
                               "status": {"privacyStatus": self.privacy, "uploadStatus": "processed"},
                               "processingDetails": {"processingStatus": "succeeded"}}]}
        raise AssertionError(resource)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.video = self.directory / "video.mp4"
        self.video.write_bytes(b"abcdefgh")
        self.item = {"key": "long", "path": str(self.video), "sha256": youtube.file_sha256(self.video),
                     "title": "Rain", "description": "Original synthetic rain; credits: channel.",
                     "made_for_kids": False, "contains_synthetic_media": True}
        self.bundle = {"schema_version": 1, "bundle_id": "daily-2026-11-03",
                       "intended_channel_id": CHANNEL_ID, "videos": [self.item]}

    def stage(self, client, **kwargs):
        return youtube.Stage(client, self.bundle, CHANNEL_ID, self.directory / "private.json",
                             self.directory / "report.json", ledger_path=self.directory / "ledger.json", **kwargs)

    def test_resumable_upload_records_id_and_keeps_session_private(self):
        client = FakeClient([
            youtube.Response(200, {"location": SESSION}, b""),
            youtube.Response(308, {}, b""),
            youtube.Response(308, {"range": "bytes=0-3"}, b""),
            youtube.Response(200, {}, json.dumps({"id": VIDEO_ID}).encode()),
        ])
        stage = self.stage(client)
        stage.journal["videos"]["long"] = {"sha256": self.item["sha256"]}
        with mock.patch.object(youtube, "CHUNK_SIZE", 4):
            self.assertEqual(stage.upload(self.item), VIDEO_ID)
        ledger = json.loads(stage.ledger_path.read_text())
        self.assertTrue(ledger["videos"]["long"]["attempt_started"])
        self.assertEqual(ledger["videos"]["long"]["video_id"], VIDEO_ID)
        self.assertNotIn("PRIVATE_SESSION", stage.ledger_path.read_text())
        self.assertNotIn("PRIVATE_SESSION", stage.report_path.read_text())
        self.assertEqual(stage.journal_path.stat().st_mode & 0o777, 0o600)
        metadata = json.loads(client.requests[0][0][2])
        self.assertEqual(metadata["status"]["privacyStatus"], "unlisted")
        self.assertTrue(metadata["status"]["containsSyntheticMedia"])
        self.assertFalse(metadata["status"]["selfDeclaredMadeForKids"])
        self.assertEqual(client.requests[2][0][2], b"abcd")
        self.assertEqual(client.requests[3][0][2], b"efgh")

    def test_ledger_sync_must_succeed_before_new_upload(self):
        client = FakeClient()
        stage = self.stage(client, sync_command=Path("/safe/ledger-sync"))
        stage.journal["videos"]["long"] = {"sha256": self.item["sha256"]}
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(youtube.YouTubeError, "persistence failed"):
                stage.upload(self.item)
        self.assertFalse(client.requests)
        ledger = json.loads(stage.ledger_path.read_text())
        self.assertTrue(ledger["videos"]["long"]["attempt_started"])

    def test_lost_success_response_queries_same_session_without_duplicate(self):
        client = FakeClient([
            youtube.Response(200, {"location": SESSION}, b""),
            youtube.Response(308, {}, b""),
            youtube.YouTubeError("network interrupted"),
            youtube.Response(200, {}, json.dumps({"id": VIDEO_ID}).encode()),
        ])
        stage = self.stage(client)
        stage.journal["videos"]["long"] = {"sha256": self.item["sha256"]}
        with mock.patch.object(youtube.time, "sleep"):
            self.assertEqual(stage.upload(self.item), VIDEO_ID)
        self.assertEqual(sum(args[0] == "POST" for args, _ in client.requests), 1)
        self.assertEqual(client.requests[-1][0][3]["Content-Range"], "bytes */8")

    def test_sync_recovered_id_prevents_new_upload(self):
        client = FakeClient()
        stage = self.stage(client, sync_command=Path("/safe/ledger-sync"))
        stage.journal["videos"]["long"] = {"sha256": self.item["sha256"]}
        def sync(*args, **kwargs):
            ledger = json.loads(stage.ledger_path.read_text())
            ledger["videos"]["long"]["video_id"] = VIDEO_ID
            youtube.atomic_json(stage.ledger_path, ledger)
            return mock.Mock(returncode=0)
        with mock.patch.object(subprocess, "run", side_effect=sync):
            self.assertEqual(stage.upload(self.item), VIDEO_ID)
        self.assertFalse(client.requests)

    def test_pending_remote_attempt_without_session_fails_closed(self):
        initial = {"schema_version": 1, "bundle_id": self.bundle["bundle_id"], "intended_channel_id": CHANNEL_ID,
                   "videos": {"long": {"sha256": self.item["sha256"], "attempt_started": True}}}
        youtube.atomic_json(self.directory / "ledger.json", initial)
        stage = self.stage(FakeClient())
        with self.assertRaisesRegex(youtube.YouTubeError, "no recovered ID/session"):
            stage.recover(self.item, [])
        self.assertFalse(stage.client.requests)

    def test_completed_remote_marker_is_reused(self):
        stage = self.stage(FakeClient())
        upload = {"id": VIDEO_ID, "snippet": {"tags": [youtube.marker(self.bundle["bundle_id"], "long"), "ambience-sha256-" + self.item["sha256"]]}}
        self.assertEqual(stage.recover(self.item, [upload]), VIDEO_ID)
        self.assertFalse(stage.client.requests)
        ledger = cloud_ledger.validate(json.loads(stage.ledger_path.read_text()))
        self.assertEqual(ledger["videos"]["long"]["video_id"], VIDEO_ID)
        self.assertTrue(ledger["videos"]["long"]["attempt_started"])

    def test_explicit_existing_id_produces_valid_safe_ledger(self):
        stage = self.stage(FakeClient(), existing={"long": VIDEO_ID})
        self.assertEqual(stage.recover(self.item, []), VIDEO_ID)
        ledger = cloud_ledger.validate(json.loads(stage.ledger_path.read_text()))
        self.assertEqual(ledger["videos"]["long"]["video_id"], VIDEO_ID)
        self.assertTrue(ledger["videos"]["long"]["attempt_started"])

    def test_recovered_marker_must_match_media_hash(self):
        stage = self.stage(FakeClient())
        upload = {"id": VIDEO_ID, "snippet": {"tags": [youtube.marker(self.bundle["bundle_id"], "long"), "ambience-sha256-wrong"]}}
        with self.assertRaisesRegex(youtube.YouTubeError, "media hash"):
            stage.recover(self.item, [upload])
        self.assertFalse(stage.client.requests)

    def test_duplicate_markers_block_new_upload(self):
        stage = self.stage(FakeClient())
        upload = {"id": VIDEO_ID, "snippet": {"tags": [youtube.marker(self.bundle["bundle_id"], "long")]}}
        with self.assertRaisesRegex(youtube.YouTubeError, "Multiple uploads"):
            stage.recover(self.item, [upload, upload])

    def test_explicit_existing_id_must_belong_to_intended_channel(self):
        client = FakeClient()
        client.owner = "UCotherchannel00000000000"
        stage = self.stage(client, existing={"long": VIDEO_ID})
        with self.assertRaisesRegex(youtube.YouTubeError, "another channel"):
            stage.recover(self.item, [])
        self.assertFalse(client.requests)

    def test_authenticated_channel_mismatch_prevents_any_upload(self):
        client = FakeClient()
        client.owner = "UCotherchannel00000000000"
        stage = self.stage(client)
        with self.assertRaisesRegex(youtube.YouTubeError, "does not match"):
            stage.run()
        self.assertFalse(client.requests)

    def test_private_visibility_is_reported_not_claimed_unlisted(self):
        client = FakeClient()
        client.privacy = "private"
        stage = self.stage(client)
        stage.journal["videos"]["long"] = {"video_id": VIDEO_ID, "sha256": self.item["sha256"]}
        stage.finish_settings(self.item, VIDEO_ID, reused=False)
        report = json.loads(stage.report_path.read_text())
        self.assertEqual(report["videos"][0]["actual_visibility"], "private")
        self.assertEqual(report["videos"][0]["studio_url"], f"https://studio.youtube.com/video/{VIDEO_ID}/edit")
        self.assertTrue(any("compliance audit" in text for text in report["videos"][0]["manual_settings"]))

    def test_public_existing_upload_is_not_silently_hidden(self):
        client = FakeClient()
        client.privacy = "public"
        stage = self.stage(client)
        stage.journal["videos"]["long"] = {"video_id": VIDEO_ID, "sha256": self.item["sha256"]}
        with self.assertRaisesRegex(youtube.YouTubeError, "already public"):
            stage.finish_settings(self.item, VIDEO_ID, reused=True)
        self.assertFalse(any(method != "GET" for method, *_ in client.api_calls))

    def test_changed_file_hash_cannot_reuse_same_upload_record(self):
        stage = self.stage(FakeClient())
        stage.journal["videos"]["long"] = {"sha256": "different", "video_id": VIDEO_ID}
        with self.assertRaisesRegex(youtube.YouTubeError, "media changed"):
            stage.recover(self.item, [])

    def test_api_refuses_non_google_session_destination(self):
        client = youtube.Client("not-a-real-token")
        with self.assertRaisesRegex(youtube.YouTubeError, "unexpected"):
            client.request("PUT", "https://example.com/steal", b"media")

    def test_private_journal_cannot_be_created_inside_repository(self):
        unsafe = Path(youtube.__file__).resolve().parent / "unsafe-session.json"
        with self.assertRaisesRegex(youtube.YouTubeError, "outside the repository"):
            youtube.Stage(FakeClient(), self.bundle, CHANNEL_ID, unsafe, self.directory / "report.json")
        self.assertFalse(unsafe.exists())

    def test_private_journal_symlink_into_repository_is_rejected(self):
        alias = self.directory / "checkout-alias"
        alias.symlink_to(Path(youtube.__file__).resolve().parent, target_is_directory=True)
        with self.assertRaisesRegex(youtube.YouTubeError, "outside the repository"):
            youtube.Stage(FakeClient(), self.bundle, CHANNEL_ID, alias / "unsafe-session.json", self.directory / "report.json")

    def test_state_path_collisions_are_rejected_before_writing(self):
        journal = self.directory / "private.json"
        report = self.directory / "report.json"
        lock = journal.with_suffix(journal.suffix + ".lock")
        for candidate_report, candidate_ledger in ((journal, None), (report, journal), (report, report), (lock, None)):
            with self.subTest(report=candidate_report, ledger=candidate_ledger):
                with self.assertRaisesRegex(youtube.YouTubeError, "distinct paths"):
                    youtube.Stage(FakeClient(), self.bundle, CHANNEL_ID, journal, candidate_report, ledger_path=candidate_ledger)
        self.assertFalse(journal.exists())

    def test_world_readable_private_journal_is_rejected(self):
        journal = self.directory / "private.json"
        journal.write_text('{}')
        journal.chmod(0o644)
        with self.assertRaisesRegex(youtube.YouTubeError, "chmod 600"):
            self.stage(FakeClient())

    def test_bundle_validation_rejects_unverified_media(self):
        bundle = {**self.bundle, "recording_credits": "Created by channel.", "videos": []}
        for key in youtube.KEYS:
            bundle["videos"].append({**self.item, "key": key, "verification": {
                "full_decode": False, "resolution": [1920, 1080] if key == "long" else [1080, 1920],
                "duration": 3600 if key == "long" else 20}})
        path = self.directory / "bundle.json"
        youtube.atomic_json(path, bundle)
        with self.assertRaisesRegex(youtube.YouTubeError, "full-playthrough"):
            youtube.validate_bundle(path)


if __name__ == "__main__":
    unittest.main()
