"""Confidential queue envelopes and safe portable-media restoration; no network."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import encrypted_queue as queue
import filebin_delivery


class EncryptedQueueTests(unittest.TestCase):
    KEY = b"unit-test-key-not-a-real-secret!!"
    BUNDLE_ID = "2026-11-05-forest-test"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="encrypted-queue-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = hashlib.sha256(self.KEY).digest()

    def fixture(self):
        artifacts = {}
        for key in queue.MEDIA_KEYS:
            is_video = key in {"long", "short1", "short2"}
            path = self.root / (key + (".mp4" if is_video else ".jpg"))
            path.write_bytes((key + " fixture media").encode())
            proof = {"resolution": [1920, 1080] if key == "long" else [1280, 720] if key == "thumbnail" else [1080, 1920]}
            if is_video:
                proof.update({"full_decode": True, "duration": 3600 if key == "long" else 25,
                              "decoded_seconds": 3600 if key == "long" else 25,
                              "video_codec": "h264", "audio_codec": "aac", "channels": 2})
            artifacts[key] = {"path": str(path), "sha256": queue.digest(path), "verification": proof}
        videos = []
        for key in ("long", "short1", "short2"):
            video = {"key": key, **copy.deepcopy(artifacts[key]), "title": "Fixture " + key,
                     "description": "Fixture description\n\nRecording credits:\nUnit-test creator; no actual media claim.",
                     "made_for_kids": False, "contains_synthetic_media": False}
            if key == "long":
                video.update({"thumbnail": artifacts["thumbnail"]["path"], "thumbnail_sha256": artifacts["thumbnail"]["sha256"]})
            else:
                video.update({"cover_frame": artifacts[key + "-cover"]["path"],
                              "cover_frame_sha256": artifacts[key + "-cover"]["sha256"], "suggested_cover_offset": 22})
            videos.append(video)
        data = {"schema_version": 1, "bundle_id": self.BUNDLE_ID, "ready": True, "artifacts": artifacts,
                "videos": videos, "requested_privacy": "unlisted", "schedule_enabled": False,
                "private_local_log": "MUST NOT ENTER ARCHIVE"}
        bundle = self.root / "bundle.json"; bundle.write_text(json.dumps(data))
        backdrop = self.root / "scene.jpg"; backdrop.write_bytes(b"image fixture")
        report = self.root / "motion.json"
        report.write_text(json.dumps({"bundle_id": self.BUNDLE_ID, "videos": {
            v["key"]: {"sha256": v["sha256"], "subject_motion_visible": True,
                       "camera_fixed": True, "matches_concept": True,
                       "review_notes": "Unit fixture, not a real playback approval."} for v in videos}}))
        return bundle, backdrop, report

    def test_bytes_authenticate_purpose_identity_and_random_nonce(self):
        data = b'{"url":"https://filebin.net/private-review"}'
        one = queue.encrypt_bytes(data, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID)
        two = queue.encrypt_bytes(data, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID)
        self.assertNotEqual(one, two)
        self.assertNotIn(data, one)
        self.assertEqual(queue.decrypt_bytes(one, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID), data)
        for key, purpose, bundle_id in ((b"x" * 32, "delivery-state", self.BUNDLE_ID),
                                        (self.key, "prepared-bundle", self.BUNDLE_ID),
                                        (self.key, "delivery-state", "other-bundle")):
            with self.assertRaises(queue.QueueError):
                queue.decrypt_bytes(one, key, purpose=purpose, bundle_id=bundle_id)

    def test_tamper_and_truncation_never_release_plaintext_or_replace_output(self):
        original = self.root / "plain"; original.write_bytes(b"private content" * 10000)
        encrypted = self.root / "sealed"
        queue.encrypt_file(original, encrypted, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID)
        valid = encrypted.read_bytes()
        for damaged in (valid[:-1], valid + b"extra", valid[:-17] + bytes([valid[-17] ^ 1]) + valid[-16:]):
            encrypted.write_bytes(damaged)
            output = self.root / "restored"
            with self.assertRaises(queue.QueueError):
                queue.decrypt_file(encrypted, output, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID)
            self.assertFalse(output.exists())
        encrypted.write_bytes(valid)
        original_bytes = original.read_bytes()
        with self.assertRaises(queue.QueueError):
            queue.decrypt_file(encrypted, original, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID)
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_portable_pack_preserves_delivery_identity_and_excludes_unselected_files(self):
        bundle, backdrop, motion = self.fixture()
        (self.root / "token.json").write_text("never include me")
        before = filebin_delivery.build_manifest(bundle, backdrop)[2]
        encrypted = self.root / "prepared.enc"
        result = queue.pack_bundle(bundle, encrypted, self.key, backdrop=backdrop, motion_report=motion)
        restored = queue.restore_bundle(encrypted, self.root / "another-runner", self.key, bundle_id=self.BUNDLE_ID)
        self.assertEqual(result["delivery_identity"], before)
        self.assertEqual(restored["delivery_identity"], before)
        new_bundle = json.loads(restored["bundle_path"].read_text())
        self.assertNotIn("private_local_log", new_bundle)
        self.assertFalse((restored["bundle_path"].parent / "token.json").exists())
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in new_bundle["artifacts"].values()))
        self.assertEqual(len(list(restored["bundle_path"].parent.iterdir())), 10)
        self.assertEqual(filebin_delivery.build_manifest(restored["bundle_path"], restored["backdrop_path"])[2], before)

    def test_unreviewed_short_prevents_packing(self):
        bundle, backdrop, motion = self.fixture()
        data = json.loads(motion.read_text()); data["videos"]["short2"]["subject_motion_visible"] = False
        motion.write_text(json.dumps(data))
        with self.assertRaisesRegex(queue.QueueError, "motion-review"):
            queue.pack_bundle(bundle, self.root / "blocked.enc", self.key, backdrop=backdrop, motion_report=motion)
        self.assertFalse((self.root / "blocked.enc").exists())

    def test_private_draft_pack_needs_no_key_and_roundtrips_portably(self):
        bundle, backdrop, motion = self.fixture()
        archive = self.root / "private-prepared.tar"
        packed = queue.pack_private_bundle(bundle, archive, backdrop=backdrop, motion_report=motion)
        self.assertEqual(packed["sha256"], queue.digest(archive))
        result = queue.restore_private_bundle(archive, self.root / "private-restored", bundle_id=self.BUNDLE_ID)
        self.assertEqual(result["delivery_identity"], packed["delivery_identity"])
        with self.assertRaises(queue.QueueError):
            queue.restore_private_bundle(archive, self.root / "wrong-date", bundle_id="other-bundle")
        self.assertFalse((self.root / "wrong-date").exists())

    def test_private_archive_rejects_external_paths_even_with_matching_hashes(self):
        bundle, backdrop, motion = self.fixture()
        archive = self.root / "portable.tar"
        queue.pack_private_bundle(bundle, archive, backdrop=backdrop, motion_report=motion)
        with tarfile.open(archive, "r:") as source:
            entries = {member.name: source.extractfile(member).read() for member in source}
        data = json.loads(entries["bundle.json"])
        data["artifacts"]["long"]["path"] = str(self.root / "long.mp4")
        next(v for v in data["videos"] if v["key"] == "long")["path"] = str(self.root / "long.mp4")
        entries["bundle.json"] = json.dumps(data).encode()
        manifest = json.loads(entries["queue-manifest.json"])
        manifest["files"]["bundle.json"] = {"size": len(entries["bundle.json"]),
            "sha256": hashlib.sha256(entries["bundle.json"]).hexdigest()}
        entries["queue-manifest.json"] = json.dumps(manifest).encode()
        with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as output:
            for name, data in entries.items():
                member = tarfile.TarInfo(name); member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        with self.assertRaises(queue.QueueError):
            queue.restore_private_bundle(archive, self.root / "external-paths", bundle_id=self.BUNDLE_ID)
        self.assertFalse((self.root / "external-paths").exists())

    def test_authenticated_archive_rejects_traversal_and_links(self):
        for malicious in ("../escaped.txt", "/tmp/escaped.txt", "linked.jpg"):
            plain = self.root / "evil.tar"
            with tarfile.open(plain, "w") as archive:
                for index in range(9):
                    member = tarfile.TarInfo(f"fixture-{index}.jpg"); member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))
                member = tarfile.TarInfo(malicious)
                if malicious == "linked.jpg":
                    member.type = tarfile.SYMTYPE; member.linkname = "/tmp/outside"
                    archive.addfile(member)
                else:
                    member.size = 1; archive.addfile(member, io.BytesIO(b"x"))
            encrypted = self.root / "evil.enc"
            encrypted.unlink(missing_ok=True)
            queue.encrypt_file(plain, encrypted, self.key, purpose="prepared-bundle", bundle_id=self.BUNDLE_ID)
            with self.assertRaises(queue.QueueError):
                queue.restore_bundle(encrypted, self.root / "unsafe", self.key, bundle_id=self.BUNDLE_ID)
            self.assertFalse((self.root / "unsafe").exists())
            with self.assertRaises(queue.QueueError):
                queue.restore_private_bundle(plain, self.root / "unsafe-plain", bundle_id=self.BUNDLE_ID)
            self.assertFalse((self.root / "unsafe-plain").exists())

    def test_key_loading_rejects_shared_permissions_and_size_limit_is_early(self):
        path = self.root / "key"
        path.write_bytes(base64.b64encode(self.key)); path.chmod(0o600)
        self.assertEqual(queue.load_key(key_file=path), self.key)
        path.chmod(0o644)
        with self.assertRaises(queue.QueueError): queue.load_key(key_file=path)
        with mock.patch.object(queue, "MAX_BYTES", 64):
            with self.assertRaisesRegex(queue.QueueError, "2 GiB"):
                queue.encrypt_bytes(b"x" * 64, self.key, purpose="delivery-state", bundle_id=self.BUNDLE_ID)


if __name__ == "__main__":
    unittest.main()
