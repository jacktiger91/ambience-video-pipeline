"""Offline bundle tests; full production hour is deliberately not synthesized here."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prepare_bundle as bundle
from render import RenderError


def episode():
    return {
        "bundle_id": "test-rain", "intended_channel_id": "UC" + "a" * 22,
        "theme": "Original rain test", "title": "Original rain for a quiet hour",
        "description": "An original synthetic ambience.",
        "recording_credits": "Locally generated test illustration and synthesized tone; no field recordings.",
        "made_for_kids": False, "contains_synthetic_media": True,
        "shorts": [
            {"title": "Quiet rain moment one", "description": "First preview.", "start": 0, "duration": 15, "framing": "left"},
            {"title": "Quiet rain moment two", "description": "Second preview.", "start": 20, "duration": 15, "framing": "right"},
        ],
    }


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="bundle-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "source.mp4"
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-filter_complex_threads", "2",
            "-f", "lavfi", "-i", "color=c=red:s=160x180:r=30:d=40",
            "-f", "lavfi", "-i", "color=c=blue:s=160x180:r=30:d=40",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=40",
            "-filter_complex", "[0:v][1:v]hstack[v]", "-map", "[v]", "-map", "2:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-ac", "2", "-t", "40", str(cls.source),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr)

    def test_two_vertical_shorts_full_decode_audio_and_invitation(self):
        config = bundle.validate_episode(episode())
        for index, expected in ((0, "red"), (1, "blue")):
            short = config["shorts"][index]
            destination = self.root / f"short{index}.mp4"
            verified = bundle.make_short(self.source, destination, short)
            self.assertTrue(verified["full_decode"])
            self.assertEqual(verified["resolution"], [1080, 1920])
            self.assertAlmostEqual(verified["duration"], 15)
            metadata = bundle.probe(destination)
            audio = bundle.stream_of_type(metadata, "audio", "test")
            self.assertEqual((audio["codec_name"], audio["channels"]), ("aac", 2))
            pixel = subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", "2", "-i", str(destination),
                "-vf", "format=rgb24,crop=1:1:540:800", "-frames:v", "1", "-threads", "1", "-f", "rawvideo", "-",
            ])
            self.assertGreater(pixel[0 if expected == "red" else 2], 180)
            invitation = subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", "12", "-i", str(destination),
                "-vf", "crop=900:220:90:1320,format=gray", "-frames:v", "1", "-threads", "1", "-f", "rawvideo", "-",
            ])
            self.assertGreater(sum(value > 180 for value in invitation), 300, "Invitation text should be visible")
            frame = self.root / f"cover{index}.jpg"
            cover = bundle.make_frame(destination, frame, short["cover_offset"], 1080, 1920)
            self.assertEqual(cover["resolution"], [1080, 1920])
            self.assertLess(frame.stat().st_size, 2 * 1024 * 1024)

    def test_invalid_and_duplicate_short_recipes_rejected(self):
        for duration in (0, 14.9, 30.1, float("inf")):
            config = episode()
            config["shorts"][0]["duration"] = duration
            with self.subTest(duration=duration), self.assertRaises(RenderError):
                bundle.validate_episode(config)
        config = episode()
        config["shorts"][1].update(start=0, framing="left")
        with self.assertRaisesRegex(RenderError, "different moments"):
            bundle.validate_episode(config)
        config = episode()
        config["shorts"][0]["start"] = 3590
        with self.assertRaises(RenderError):
            bundle.validate_episode(config)

    def test_fractional_crops_validate_and_preserve_legacy_defaults(self):
        config = bundle.validate_episode(episode())
        self.assertEqual(bundle.horizontal_crop(config["shorts"][0]), 0)
        self.assertEqual(bundle.horizontal_crop(config["shorts"][1]), 1)
        legacy = config["shorts"][0]
        self.assertEqual(bundle.short_recipe(legacy), [legacy["start"], legacy["duration"], "left", bundle.INVITATION])
        for bad in (-0.01, 1.01, float("nan"), True):
            config = episode()
            config["shorts"][0]["crop_position"] = bad
            with self.subTest(position=bad), self.assertRaises(RenderError):
                bundle.validate_episode(config)
        # Aliases of the same effective framing must not pass the distinctness check.
        config = episode()
        config["shorts"][1].update(start=0, framing="center", crop_position=0)
        with self.assertRaisesRegex(RenderError, "different moments"):
            bundle.validate_episode(config)

    def test_fractional_crops_follow_requested_horizontal_position(self):
        source = self.root / "gradient.mp4"
        bundle.run_ffmpeg([
            "-f", "lavfi", "-i", "nullsrc=s=320x180:r=30:d=16,geq=lum='X/W*255':cb=128:cr=128",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=16",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "10", "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "aac", "-ac", "2", str(source),
        ])
        def gray_pixel(path, x, y):
            return subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", "2", "-i", str(path),
                "-vf", f"format=gray,crop=1:1:{x}:{y}", "-frames:v", "1", "-threads", "1", "-f", "rawvideo", "-",
            ])[0]
        for index, position in enumerate((0.19, 0.85)):
            short = {"start": 0, "duration": 15, "framing": "center", "crop_position": position, "cta": ("related", "quiet-hours")[index]}
            destination = self.root / f"fractional-{index}.mp4"
            bundle.make_short(source, destination, short)
            # Scale-to-fill gives a 3414-wide frame before cropping. Compare the
            # resulting center pixel with its corresponding source location.
            source_x = round((position * (3414 - 1080) + 540) / 3414 * 320)
            self.assertAlmostEqual(gray_pixel(destination, 540, 800), gray_pixel(source, source_x, 90), delta=4)
        self.assertNotEqual(bundle.short_recipe({**short, "crop_position": 0.19}), bundle.short_recipe(short))

    def test_finished_thumbnail_normalization_is_deterministic_and_hash_bound(self):
        artwork = self.root / "finished-thumbnail.png"
        bundle.run_ffmpeg(["-i", str(self.source), "-vf", "scale=400:100", "-frames:v", "1", "-threads", "1", str(artwork)])
        original_hash = bundle.digest(artwork)
        recipe = bundle.finished_thumbnail_recipe(artwork)
        first, second = self.root / "normalized-one.jpg", self.root / "normalized-two.jpg"
        verification = bundle.make_finished_thumbnail(artwork, first)
        bundle.make_finished_thumbnail(artwork, second)
        self.assertEqual(bundle.digest(first), bundle.digest(second))
        self.assertEqual(bundle.digest(artwork), original_hash, "Normalization must preserve the finished source artwork")
        self.assertEqual(verification["resolution"], [1280, 720])
        self.assertEqual(verification["source_sha256"], original_hash)
        self.assertLess(first.stat().st_size, 2 * 1024 * 1024)
        for x, y, expected in ((100, 360, "red"), (1180, 360, "blue"), (640, 20, "black")):
            pixel = subprocess.check_output([
                "ffmpeg", "-v", "error", "-i", str(first), "-vf", f"format=rgb24,crop=1:1:{x}:{y}",
                "-frames:v", "1", "-threads", "1", "-f", "rawvideo", "-",
            ])
            if expected == "black":
                self.assertLess(max(pixel), 20, "A wide image should be padded, preserving its complete artwork")
            else:
                self.assertGreater(pixel[0 if expected == "red" else 2], 180)
        manifest = {"artifacts": {"thumbnail": {"sha256": bundle.digest(first), "recipe_sha256": bundle.json_digest(recipe)}}}
        self.assertIsNotNone(bundle.cached_artifact(manifest, "thumbnail", first, bundle.json_digest(recipe)))
        # Same path, different artwork: content identity must invalidate reuse.
        bundle.run_ffmpeg(["-f", "lavfi", "-i", "color=green:s=400x100", "-frames:v", "1", "-threads", "1", "-y", str(artwork)])
        replacement_recipe = bundle.finished_thumbnail_recipe(artwork)
        self.assertNotEqual(recipe, replacement_recipe)
        with self.assertRaisesRegex(RenderError, "recipe"):
            bundle.cached_artifact(manifest, "thumbnail", first, bundle.json_digest(replacement_recipe))

    def test_explicit_credits_disclosures_and_channel_required(self):
        for key in ("recording_credits", "contains_synthetic_media", "made_for_kids", "intended_channel_id"):
            config = episode()
            del config[key]
            with self.subTest(key=key), self.assertRaises(RenderError):
                bundle.validate_episode(config)
        config = episode()
        config["intended_channel_id"] = None
        config["intended_channel_handle"] = "@QuietHoursRelaxingSounds"
        validated = bundle.validate_episode(config)
        self.assertIsNone(validated["intended_channel_id"])

    def test_existing_or_modified_artifacts_are_never_overwritten(self):
        path = self.root / "kept.mp4"
        path.write_bytes(b"keep this file")
        with self.assertRaisesRegex(RenderError, "Untracked output"):
            bundle.cached_artifact({}, "short1", path, "recipe")
        manifest = {"artifacts": {"short1": {"sha256": bundle.digest(path), "recipe_sha256": "recipe"}}}
        self.assertIsNotNone(bundle.cached_artifact(manifest, "short1", path, "recipe"))
        with self.assertRaisesRegex(RenderError, "recipe"):
            bundle.cached_artifact(manifest, "short1", path, "different-recipe")
        path.write_bytes(b"modified file")
        with self.assertRaisesRegex(RenderError, "changed"):
            bundle.cached_artifact(manifest, "short1", path, "recipe")

    def test_production_long_must_be_one_hour_and_1080p(self):
        with self.assertRaises(RenderError):
            bundle.verify_media(self.source, 1920, 1080, 3600)

    def test_sha_matched_source_proof_reuses_full_decode_only(self):
        proof = {"sha256": bundle.digest(self.source), "duration": 40.066667, "full_decode_passed": True}
        with patch("prepare_bundle.run_ffmpeg") as decode:
            result = bundle.verify_media(self.source, 320, 180, 40, proof)
            self.assertTrue(result["reused_source_full_decode"])
            decode.assert_not_called()
        proof["decoded_seconds"] = 40.021
        with patch("prepare_bundle.run_ffmpeg") as decode:
            result = bundle.verify_media(self.source, 320, 180, 40, proof)
            self.assertEqual(result["decoded_seconds"], 40.021, "Preserve the actual decode endpoint rather than substituting header duration")
            decode.assert_not_called()
        proof["decoded_seconds"] = 39
        with patch("prepare_bundle.run_ffmpeg", side_effect=RenderError("decode attempted")):
            with self.assertRaisesRegex(RenderError, "decode attempted"):
                bundle.verify_media(self.source, 320, 180, 40, proof)
        proof["decoded_seconds"] = 40.021
        proof["sha256"] = "a different file"
        with patch("prepare_bundle.run_ffmpeg", side_effect=RenderError("decode attempted")):
            with self.assertRaisesRegex(RenderError, "decode attempted"):
                bundle.verify_media(self.source, 320, 180, 40, proof)

    def test_descriptions_include_actual_credits_and_fit_limit(self):
        text = bundle.description("Rain preview", "Synthetic sound, original illustration", True)
        self.assertIn(bundle.INVITATION, text)
        self.assertIn("Recording credits", text)
        with self.assertRaises(RenderError):
            bundle.description("x" * 4999, "Credits", True)
        config = episode()
        config["shorts"][0]["recording_credits"] = "The actual excerpt author; CC0, https://example.com/source"
        validated = bundle.validate_episode(config)
        self.assertEqual(validated["shorts"][0]["recording_credits"], config["shorts"][0]["recording_credits"])
        config["shorts"][0]["recording_credits"] = ""
        with self.assertRaises(RenderError):
            bundle.validate_episode(config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
