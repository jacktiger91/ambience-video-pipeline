"""Local-date/deadline/retry decisions; no network, media rendering or uploads."""
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import daily_schedule as schedule


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="schedule-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "schedule.json"
        self.catalog = {"rotation_start_date": "2026-11-04", "concepts": [{"id": "shore"}, {"id": "forest"}]}
        self.config = {"schema_version": 1, "enabled": True, "timezone": "Asia/Nicosia",
                       "attempt_from": "17:00", "ready_by": "18:00", "concept_catalog": "concepts.json",
                       "prepared_episodes": {}}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.config))
        (self.root / "concepts.json").write_text(json.dumps(self.catalog))

    def inspect(self, now):
        return schedule.assess(self.path, schedule.aware_time(now))

    def prepared(self, day="2026-11-04", concept="shore"):
        bundle_id = day + "-" + concept
        self.config["prepared_episodes"][day] = {
            "bundle_id": bundle_id, "bundle": "bundle.json", "backdrop": "scene.jpg",
            "motion_review": "motion.json", "receipt": "receipt.json",
        }
        self.write()
        videos = [{"key": name, "sha256": str(index)*64} for index, name in enumerate(("long", "short1", "short2"), 1)]
        bundle = {"bundle_id": bundle_id, "videos": videos}
        report = {"bundle_id": bundle_id, "videos": {v["key"]: {
            "sha256": v["sha256"], "subject_motion_visible": True, "camera_fixed": True,
            "matches_concept": True, "review_notes": "Unit fixture only, not an actual media approval."
        } for v in videos}}
        (self.root / "motion.json").write_text(json.dumps(report))
        files = {bundle_id+"-long.mp4": {"size": 42, "sha256": "a"*64}}
        return bundle, files, "f"*64

    def test_local_date_and_no_previous_day_fallback(self):
        self.prepared()
        result = self.inspect("2026-11-04T22:15:00Z")
        self.assertEqual(result["episode_date"], "2026-11-05")
        self.assertEqual(result["concept_id"], "forest")
        self.assertFalse(result["local_package_ready"])
        self.assertFalse(result["eligible_for_attempt"])
        self.assertIn("no restored", result["blockers"][-1])
        exhausted = self.inspect("2026-11-06T15:30:00Z")
        self.assertNotIn("bundle_id", exhausted)
        self.assertIn("do not recycle", exhausted["blockers"][-1])

    def test_cyprus_deadline_tracks_summer_and_winter_offsets(self):
        winter = self.inspect("2026-11-04T15:30:00Z")
        self.assertEqual(winter["ready_by_utc"], "2026-11-04T16:00:00+00:00")
        self.catalog["rotation_start_date"] = "2026-07-04"
        self.write()
        summer = self.inspect("2026-07-04T14:30:00Z")
        self.assertEqual(summer["ready_by_utc"], "2026-07-04T15:00:00+00:00")
        self.assertEqual(summer["ready_by"], "2026-07-04T18:00:00+03:00")

    def test_ready_window_and_same_day_late_retry_are_distinguished(self):
        prepared = self.prepared()
        with mock.patch.object(schedule.filebin_delivery, "build_manifest", return_value=prepared):
            early = self.inspect("2026-11-04T14:59:00Z")
            self.assertTrue(early["local_package_ready"])
            self.assertFalse(early["eligible_for_attempt"])
            due = self.inspect("2026-11-04T15:10:00Z")
            self.assertTrue(due["eligible_for_attempt"])
            self.assertFalse(due["deadline_passed"])
            late = self.inspect("2026-11-04T16:10:00Z")
            self.assertTrue(late["eligible_for_attempt"])
            self.assertTrue(late["deadline_passed"])
            self.config["enabled"] = False
            self.write()
            disabled = self.inspect("2026-11-04T15:10:00Z")
            self.assertTrue(disabled["local_package_ready"])
            self.assertFalse(disabled["eligible_for_attempt"])

    def test_source_expiry_and_short_motion_review_block_readiness(self):
        prepared = self.prepared()
        self.config["prepared_episodes"]["2026-11-04"]["source_available_until"] = "2026-11-04T15:45:00Z"
        self.write()
        with mock.patch.object(schedule.filebin_delivery, "build_manifest", return_value=prepared) as build:
            expired = self.inspect("2026-11-04T15:10:00Z")
            self.assertIn("expires before", expired["blockers"][-1])
            build.assert_not_called()
            del self.config["prepared_episodes"]["2026-11-04"]["source_available_until"]
            self.write()
            report = json.loads((self.root / "motion.json").read_text())
            report["videos"]["short2"]["subject_motion_visible"] = False
            (self.root / "motion.json").write_text(json.dumps(report))
            result = self.inspect("2026-11-04T15:10:00Z")
            self.assertFalse(result["local_package_ready"])
            self.assertIn("All three final exports", result["blockers"][-1])

    def test_completed_receipt_prevents_repeat_even_after_link_expiry(self):
        bundle, files, identity = self.prepared()
        expected = copy.deepcopy(files)
        name = next(iter(files))
        bin_id = "b"*32
        receipt = {"schema_version": 1, "provider": "filebin.net", "bin": bin_id,
                   "bundle_id": bundle["bundle_id"], "identity": identity,
                   "expected_files": expected, "files": {name: {
                       **expected[name], "url": "https://filebin.net/"+bin_id+"/"+name,
                       "uploaded": True, "download_verified": True,
                       "download_verification_method": "one complete HTTP GET, SHA-256 and byte count"}},
                   "readonly": True, "status": "delivered", "expires_at": "2026-11-04T10:00:00Z"}
        path = self.root / "receipt.json"
        path.write_text(json.dumps(receipt))
        original = path.read_bytes()
        with mock.patch.object(schedule.filebin_delivery, "build_manifest", return_value=(bundle, files, identity)):
            one = self.inspect("2026-11-04T15:10:00Z")
            two = self.inspect("2026-11-04T15:10:00Z")
            self.assertEqual(one, two)
            self.assertEqual(one["receipt_status"], "delivered")
            self.assertTrue(one["local_package_ready"])
            self.assertFalse(one["eligible_for_attempt"])
            self.assertEqual(path.read_bytes(), original)

    def test_wrong_identity_or_naive_clock_is_rejected(self):
        self.prepared()
        self.config["prepared_episodes"]["2026-11-04"]["bundle_id"] = "2026-11-03-old-shore"
        self.write()
        with self.assertRaisesRegex(schedule.ScheduleError, "different day"):
            self.inspect("2026-11-04T15:10:00Z")
        with self.assertRaisesRegex(schedule.ScheduleError, "timezone-aware"):
            schedule.assess(self.path, datetime(2026,11,4,17,10))
        with self.assertRaisesRegex(schedule.ScheduleError, "timezone is required"):
            schedule.aware_time("2026-11-04T17:10:00")


if __name__ == "__main__":
    unittest.main()
