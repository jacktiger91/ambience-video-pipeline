import copy
import unittest

from cloud_ledger import LedgerError, merge, validate


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.record = {"schema_version": 1, "bundle_id": "night-rain-2026-11-03", "intended_channel_id": "UC" + "a" * 22,
                       "videos": {"long": {"sha256": "a" * 64, "attempt_started": True}}}

    def test_credential_and_session_fields_rejected(self):
        for key in ("refresh_token", "access_token", "session_url", "upload_url"):
            with self.subTest(key=key):
                record = copy.deepcopy(self.record)
                record["videos"]["long"][key] = "secret"
                with self.assertRaises(LedgerError):
                    validate(record)

    def test_pending_and_success_survive_stale_retry(self):
        remote = copy.deepcopy(self.record)
        remote["videos"]["long"]["video_id"] = "abcdefghijk"
        stale = copy.deepcopy(self.record)
        stale["videos"]["long"]["attempt_started"] = False
        result = merge(remote, stale)
        self.assertTrue(result["videos"]["long"]["attempt_started"])
        self.assertEqual(result["videos"]["long"]["video_id"], "abcdefghijk")

    def test_conflicting_identity_content_and_ids_rejected(self):
        remote = copy.deepcopy(self.record)
        remote["videos"]["long"]["video_id"] = "abcdefghijk"
        for kind in ("sha256", "video_id", "channel"):
            local = copy.deepcopy(remote)
            if kind == "channel":
                local["intended_channel_id"] = "UC" + "b" * 22
            else:
                local["videos"]["long"][kind] = "b" * 64 if kind == "sha256" else "12345678901"
            with self.subTest(kind=kind), self.assertRaises(LedgerError):
                merge(remote, local)

    def test_missing_remote_items_are_preserved(self):
        local = copy.deepcopy(self.record)
        local["videos"] = {"short1": {"sha256": "b" * 64, "attempt_started": True}}
        self.assertEqual(set(merge(self.record, local)["videos"]), {"long", "short1"})


if __name__ == "__main__":
    unittest.main()
