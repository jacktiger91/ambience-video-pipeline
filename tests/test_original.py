"""Meaningful short checks for the original, free procedural content generator."""

import array
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import create_original


class OriginalContentTests(unittest.TestCase):
    def test_illustration_is_deterministic_and_themes_are_distinct(self):
        first = create_original.make_scene("night-rain", 1)
        self.assertEqual(first, create_original.make_scene("night-rain", 1))
        self.assertNotEqual(first, create_original.make_scene("night-rain", 2))
        artworks = [create_original.make_scene(theme, 1) for theme in create_original.THEMES]
        self.assertEqual(len(set(artworks)), 7)
        self.assertNotIn("http://", first.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("<image", first)

    def test_short_original_reuses_verified_output_and_protects_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="original-smoke-") as directory:
            root = Path(directory)
            output = root / "original.mp4"
            assets = root / "assets"
            result = create_original.create("night-rain", 123, 1.0, output, assets)
            self.assertTrue(result["verification"]["full_decode_passed"])
            self.assertAlmostEqual(result["verification"]["duration"], 1.0, delta=.1)
            self.assertIn("No field recordings", result["recording_credits"])
            before = output.stat().st_mtime_ns
            self.assertEqual(result, create_original.create("night-rain", 123, 1.0, output, assets))
            self.assertEqual(before, output.stat().st_mtime_ns)
            with self.assertRaises(create_original.RenderError):
                create_original.create("night-rain", 124, 1.0, output, assets)
            self.assertEqual(before, output.stat().st_mtime_ns)
            # Decode audio and prove both stereo channels contain independent,
            # nonzero samples, rather than silence or duplicated mono audio.
            raw = subprocess.check_output([
                "ffmpeg", "-v", "error", "-i", str(output), "-ss", "0.3", "-t", "0.4",
                "-vn", "-ar", "8000", "-f", "s16le", "pipe:1",
            ])
            samples = array.array("h", raw)
            if sys.byteorder != "little":
                samples.byteswap()
            left, right = samples[::2], samples[1::2]
            self.assertGreater(len(left), 1000)
            self.assertGreater(max(abs(x) for x in left), 100)
            self.assertGreater(max(abs(x) for x in right), 100)
            self.assertNotEqual(left, right)


if __name__ == "__main__":
    unittest.main(verbosity=2)
