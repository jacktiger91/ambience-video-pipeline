"""Cloud preparation guardrails; no credentials, network, or hour-long renders."""

import argparse
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import daily


class DailyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="daily-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_download_does_not_expose_secret_url_on_failure(self):
        secret_url = "https://example.invalid/private?token=do-not-log-this"
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(secret_url)
        with mock.patch.dict(os.environ, {"SOURCE_LONG_VIDEO_URL": secret_url}), \
                mock.patch("daily.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(daily.JobError) as raised:
                daily.download("SOURCE_LONG_VIDEO_URL", self.directory / "source.mp4")
        self.assertNotIn("do-not-log-this", str(raised.exception))
        self.assertIn("SOURCE_LONG_VIDEO_URL", str(raised.exception))

    def test_download_rejects_plain_http(self):
        with mock.patch.dict(os.environ, {"SOURCE_LONG_VIDEO_URL": "http://example.invalid/video"}):
            with self.assertRaises(daily.JobError):
                daily.download("SOURCE_LONG_VIDEO_URL", self.directory / "source.mp4")

    def test_artifact_gate_requires_zero_spend_and_verified_manifest(self):
        args = argparse.Namespace(directory=self.directory, max_mib=1)
        with mock.patch.dict(os.environ, {"ZERO_SPEND_CONFIRMED": ""}):
            with self.assertRaises(daily.JobError):
                daily.check_artifact_budget(args)
        with mock.patch.dict(os.environ, {"ZERO_SPEND_CONFIRMED": "true"}):
            with self.assertRaises(daily.JobError):
                daily.check_artifact_budget(args)
            (self.directory / "bundle.json").write_text("{}")
            with mock.patch("sys.stdout", new=io.StringIO()):
                daily.check_artifact_budget(args)
            (self.directory / "too-large.mp4").write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaises(daily.JobError):
                daily.check_artifact_budget(args)

    def catalog(self, length=14):
        catalog = {
            "schema_version": 1, "rotation_start_date": "2026-11-04",
            "brand": {"name": "Quiet Hours", "style_guide": "docs/CHANNEL-STYLE.md"},
            "concepts": [{
                "id": f"concept-{index}", "title": f"Distinct sound {index}", "theme": f"Setting {index}",
                "sound_family": ("water", "wildlife", "rain", "seaside")[index % 4],
                "setting": f"Location type {index}", "framing": f"Composition {index}",
                "backdrop_brief": f"Create a different photographic scene number {index}.",
                "thumbnail_brief": f"New scene {index}, same branded typography.",
                "audio_requirements": {"kind": "real_field_recording", "required_sounds": [f"sound-{index}"],
                    "excluded_sounds": ["speech", "traffic"], "license_requirement": "Verified rights for YouTube use"},
            } for index in range(length)],
        }
        path = self.directory / "catalog.json"
        path.write_text(json.dumps(catalog))
        return path

    def plan_args(self, catalog, day="2026-11-04", filename="brief.json"):
        return argparse.Namespace(catalog=catalog, date=day, output=self.directory / filename)

    def test_fourteen_distinct_truthful_briefs_and_no_wraparound(self):
        catalog = self.catalog()
        planned = []
        for index in range(14):
            day = date.fromordinal(date(2026, 11, 4).toordinal() + index)
            args = self.plan_args(catalog, day.isoformat(), f"{index}.json")
            with mock.patch("sys.stdout", new=io.StringIO()):
                daily.plan(args)
            planned.append(json.loads(args.output.read_text()))
        self.assertEqual(len({item["backdrop_brief"] for item in planned}), 14)
        self.assertEqual(len({item["bundle_id"] for item in planned}), 14)
        self.assertTrue(all(item["content_status"] == "awaiting_verified_assets" for item in planned))
        self.assertTrue(all(item["recording_credits"] is None for item in planned))
        self.assertTrue(all(not item["schedule_enabled"] for item in planned))
        self.assertTrue(all(item["assets"]["audio"]["kind"] == "real_field_recording" for item in planned))
        self.assertTrue(all(item["assets"]["backdrop"]["path"] is None for item in planned))
        for day in ("2026-11-03", "2026-11-18"):
            with self.assertRaisesRegex(daily.JobError, "never silently recycled"):
                daily.plan(self.plan_args(catalog, day, "outside.json"))

    def test_plan_retry_preserves_review_notes_and_refuses_changed_brief(self):
        catalog = self.catalog()
        args = self.plan_args(catalog)
        with mock.patch("sys.stdout", new=io.StringIO()):
            daily.plan(args)
        data = json.loads(args.output.read_text())
        data["review_notes"].append("User has reviewed the new shore scene.")
        args.output.write_text(json.dumps(data))
        original = args.output.read_bytes()
        with mock.patch("sys.stdout", new=io.StringIO()):
            daily.plan(args)
        self.assertEqual(args.output.read_bytes(), original)
        with self.assertRaises(daily.JobError):
            daily.plan(self.plan_args(catalog, "2026-11-05"))
        self.assertEqual(args.output.read_bytes(), original)

    def test_default_plan_date_uses_nicosia_calendar(self):
        args = self.plan_args(self.catalog(), None)
        with mock.patch("daily.datetime") as clock, mock.patch("sys.stdout", new=io.StringIO()):
            clock.now.return_value = datetime(2026, 11, 4, 0, 30, tzinfo=daily.ZoneInfo("Asia/Nicosia"))
            daily.plan(args)
            self.assertEqual(str(clock.now.call_args.args[0]), "Asia/Nicosia")
        self.assertEqual(json.loads(args.output.read_text())["episode_date"], "2026-11-04")

    def test_catalog_rejects_synthetic_audio_and_renamed_scene(self):
        catalog = self.catalog()
        data = json.loads(catalog.read_text())
        data["concepts"][0]["audio_requirements"]["kind"] = "synthetic_noise"
        catalog.write_text(json.dumps(data))
        with self.assertRaises(daily.JobError):
            daily.load_catalog(catalog)
        data["concepts"][0]["audio_requirements"]["kind"] = "real_field_recording"
        data["concepts"][1]["backdrop_brief"] = data["concepts"][0]["backdrop_brief"]
        catalog.write_text(json.dumps(data))
        with self.assertRaises(daily.JobError):
            daily.load_catalog(catalog)

    def approved_assets(self):
        episode = self.directory / "approved.json"
        history = self.directory / "history.json"
        history.write_text('{"schema_version": 1, "episodes": {}}')
        evidence = self.directory / "rights.txt"
        evidence.write_text("Fixture documented rights review, not a real licensing assertion.")
        qa = self.directory / "qa.txt"
        qa.write_text("Fixture review completed.")
        data = {"bundle_id": "2026-11-04-shore", "episode_date": "2026-11-04", "assets": {}}
        for name, kind in (("audio", "real_field_recording"), ("backdrop", "photographic_ai_scene"), ("thumbnail", "branded_thumbnail")):
            asset = self.directory / f"{name}.fixture"
            asset.write_bytes((name + " distinct fixture").encode())
            data["assets"][name] = {
                "kind": kind, "status": "verified", "path": asset.name,
                "sha256": daily.file_digest(asset), "credits": f"Actual {name} fixture creator",
                "rights_verified": True, "rights_evidence_path": evidence.name,
                "qa_passed": True, "qa_report_path": qa.name,
            }
        data["assets"]["backdrop"]["scene_id"] = "new-shore-cove"
        data["assets"]["thumbnail"]["source_scene_id"] = "new-shore-cove"
        episode.write_text(json.dumps(data))
        return episode, data, history

    def test_assets_require_real_audio_rights_and_quality_evidence(self):
        episode, data, history = self.approved_assets()
        for field, bad in (("kind", "synthetic_noise"), ("status", "unverified"), ("rights_verified", False), ("qa_passed", False)):
            original = data["assets"]["audio"][field]
            data["assets"]["audio"][field] = bad
            with self.subTest(field=field), self.assertRaises(daily.JobError):
                daily.validate_assets(episode, data, history)
            data["assets"]["audio"][field] = original
        (self.directory / "rights.txt").unlink()
        with self.assertRaises(daily.JobError):
            daily.validate_assets(episode, data, history)

    def test_new_title_cannot_reuse_old_backdrop_but_same_bundle_retry_can(self):
        episode, data, history = self.approved_assets()
        verified = daily.validate_assets(episode, data, history)
        daily.record_asset_use(data, verified, history)
        self.assertEqual(daily.validate_assets(episode, data, history)["backdrop"]["sha256"], verified["backdrop"]["sha256"])
        data["bundle_id"] = "2026-11-05-a-different-title"
        data["title"] = "New title on yesterday's scene"
        # Supply different sound bytes so the independent backdrop check is reached.
        (self.directory / "audio.fixture").write_bytes(b"new genuine sound fixture")
        data["assets"]["audio"]["sha256"] = daily.file_digest(self.directory / "audio.fixture")
        with self.assertRaisesRegex(daily.JobError, "backdrop is already used"):
            daily.validate_assets(episode, data, history)

    def test_changed_file_and_mismatched_thumbnail_scene_are_rejected(self):
        episode, data, history = self.approved_assets()
        data["assets"]["thumbnail"]["source_scene_id"] = "old-veranda"
        with self.assertRaisesRegex(daily.JobError, "thumbnail must depict"):
            daily.validate_assets(episode, data, history)
        data["assets"]["thumbnail"]["source_scene_id"] = "new-shore-cove"
        (self.directory / "backdrop.fixture").write_bytes(b"silently replaced image")
        with self.assertRaisesRegex(daily.JobError, "SHA-256"):
            daily.validate_assets(episode, data, history)

    def test_missing_history_and_unverified_brief_never_start_render(self):
        episode, data, history = self.approved_assets()
        with self.assertRaises(daily.JobError):
            daily.validate_assets(episode, data, None)
        args = self.plan_args(self.catalog())
        with mock.patch("sys.stdout", new=io.StringIO()):
            daily.plan(args)
        prepare = argparse.Namespace(episode=args.output, output_dir=self.directory / "exports", source_mode="verified-assets",
                                     asset_history=history, legacy_synthetic=False, long_video=None, loop_audio=False)
        with mock.patch("daily.run") as run, self.assertRaises(daily.JobError):
            daily.prepare(prepare)
        run.assert_not_called()

    def test_legacy_synthetic_mode_needs_explicit_comparison_opt_in(self):
        episode = self.directory / "old.json"
        episode.write_text('{"bundle_id": "old-test"}')
        args = argparse.Namespace(episode=episode, output_dir=self.directory / "out", source_mode="generate-original",
                                  legacy_synthetic=False, asset_history=None, long_video=None, loop_audio=False)
        with mock.patch("daily.run") as run, self.assertRaisesRegex(daily.JobError, "comparison only"):
            daily.prepare(args)
        run.assert_not_called()

    def test_daily_motion_rejects_still_artwork_and_missing_review(self):
        episode = self.directory / "episode.json"
        with self.assertRaisesRegex(daily.JobError, "moving visual"):
            daily.validate_moving_visual(episode, {}, "shore")
        image = self.directory / "backdrop.png"
        image.write_bytes(b"still-image-fixture")
        record = {"status": "verified", "path": image.name, "sha256": daily.file_digest(image)}
        with self.assertRaisesRegex(daily.JobError, "still images"):
            daily.validate_moving_visual(episode, {"moving_visual": record}, "shore")

    def test_moving_video_requires_matching_subject_review_and_loop_check(self):
        episode = self.directory / "episode.json"
        video = self.directory / "moving.mp4"
        video.write_bytes(b"video-unit-fixture-with-probe-mocked")
        sha = daily.file_digest(video)
        report = {"visual_sha256": sha, "scene_id": "shore", "subject_motion_visible": True,
                  "camera_fixed": True, "matches_concept": True,
                  "review_notes": "Unit fixture, not an actual media approval."}
        report_path = self.directory / "motion-qa.json"
        report_path.write_text(json.dumps(report))
        data = {"moving_visual": {"status": "verified", "path": video.name, "sha256": sha,
                "scene_id": "shore", "qa_report_path": report_path.name}}
        metadata = {"format": {"format_name": "mov,mp4"}, "streams": [
            {"codec_type": "video", "duration": "10", "nb_frames": "300"}]}
        with mock.patch("render.probe", return_value=metadata):
            with self.assertRaisesRegex(daily.JobError, "loop transition"):
                daily.validate_moving_visual(episode, data, "shore")
            report["loop_seam_reviewed"] = True
            report_path.write_text(json.dumps(report))
            self.assertEqual(daily.validate_moving_visual(episode, data, "shore")["path"], video)
            report["subject_motion_visible"] = False
            report_path.write_text(json.dumps(report))
            with self.assertRaisesRegex(daily.JobError, "visible subject motion"):
                daily.validate_moving_visual(episode, data, "shore")


if __name__ == "__main__":
    unittest.main()
