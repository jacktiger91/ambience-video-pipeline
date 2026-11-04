#!/usr/bin/env python3
"""Exercise the real renderer and FFmpeg with disposable, generated fixtures.

Run from any directory: python3 /path/to/ambience-video-pipeline/tests/smoke_test.py
No source media, service accounts, network access, or third-party Python packages
are required. A few seconds of 1080p media are rendered, never a full hour.
"""

import array
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "render.py"


class RendererSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for executable in ("ffmpeg", "ffprobe"):
            if shutil.which(executable) is None:
                raise RuntimeError(f"{executable} is required; see README.md for setup")
        cls.temporary = tempfile.TemporaryDirectory(prefix="ambience-smoke-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        # Spaces exercise argument passing without relying on shell quoting.
        cls.still = cls.directory / "still image.png"
        cls.jpeg = cls.directory / "full range image.jpg"
        cls.clip = cls.directory / "video clip.mp4"
        cls.audio = cls.directory / "finished soundtrack.wav"
        cls.short_audio = cls.directory / "short soundtrack.wav"
        cls.ffmpeg(
            "-f", "lavfi", "-i", "color=c=red:s=160x120:r=30",
            "-frames:v", "1", "-threads", "1", str(cls.still),
        )
        # JPEG conventionally uses full-range YUV. The output still needs
        # standard yuv420p, rather than inheriting yuvj420p metadata.
        cls.ffmpeg(
            "-f", "lavfi", "-i", "color=c=red:s=160x120:r=30",
            "-frames:v", "1", "-c:v", "mjpeg", "-pix_fmt", "yuvj420p",
            "-threads", "1", str(cls.jpeg),
        )
        # A 0.4-second portrait clip alternates red and blue and carries an
        # 880 Hz tone. Rendered output must repeat its colors but use the
        # separate 440 Hz soundtrack instead of this embedded audio.
        cls.ffmpeg(
            "-f", "lavfi", "-i", "color=c=red:s=120x160:r=30:d=0.2",
            "-f", "lavfi", "-i", "color=c=blue:s=120x160:r=30:d=0.2",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=0.4",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a:0", "-t", "0.4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-threads", "2", "-c:a", "aac", str(cls.clip),
        )
        for destination, duration in ((cls.audio, "2"), (cls.short_audio, "0.25")):
            cls.ffmpeg(
                "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
                "-c:a", "pcm_s16le", str(destination),
            )

    @staticmethod
    def ffmpeg(*arguments):
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-filter_threads", "2", "-filter_complex_threads", "2", *arguments],
            capture_output=True, timeout=120,
        )
        if result.returncode:
            raise AssertionError(result.stderr.decode(errors="replace"))
        return result.stdout

    def invoke(self, *arguments):
        return subprocess.run(
            [sys.executable, str(RENDERER), *map(str, arguments)],
            capture_output=True, text=True, timeout=120, cwd=self.directory,
        )

    def render(self, name, *, visual=None, audio=None, duration="1", extra=()):
        output = self.directory / name
        result = self.invoke(
            "--visual", visual or self.still,
            "--audio", audio or self.audio,
            "--duration", duration, "--output", output, *extra,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(output.is_file(), result.stdout + result.stderr)
        return output

    def check_export(self, output, expected_duration):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(output)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        info = json.loads(result.stdout)
        videos = [s for s in info["streams"] if s["codec_type"] == "video"]
        audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(info["streams"]), 2)
        self.assertEqual(len(videos), 1)
        self.assertEqual(len(audios), 1)
        video, audio = videos[0], audios[0]
        self.assertEqual((video["width"], video["height"]), (1920, 1080))
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual(video["sample_aspect_ratio"], "1:1")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertEqual(audio["channels"], 2)
        self.assertEqual(audio["channel_layout"], "stereo")
        for actual in (info["format"]["duration"], video["duration"], audio["duration"]):
            self.assertAlmostEqual(float(actual), expected_duration, delta=0.1)

    def pixel(self, output, time, x, y):
        pixel = self.ffmpeg(
            "-ss", str(time), "-i", str(output),
            "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-frames:v", "1",
            "-an", "-threads", "1", "-f", "rawvideo", "pipe:1",
        )
        self.assertEqual(len(pixel), 3, "Expected one decoded RGB pixel")
        return tuple(pixel)

    def assert_red(self, pixel):
        red, green, blue = pixel
        self.assertGreater(red, 180)
        self.assertLess(green, 40)
        self.assertLess(blue, 40)

    def assert_blue(self, pixel):
        red, green, blue = pixel
        self.assertGreater(blue, 180)
        self.assertLess(red, 40)
        self.assertLess(green, 40)

    def check_soundtrack(self, output, time=0.1):
        raw = self.ffmpeg(
            "-ss", str(time), "-i", str(output), "-t", "0.15", "-vn",
            "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1",
        )
        samples = array.array("h", raw)
        if sys.byteorder != "little":
            samples.byteswap()
        self.assertGreater(len(samples), 800)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        self.assertGreater(rms, 100, "Soundtrack must be audible, including after a loop")

        def magnitude(frequency):
            scale = 2 * math.pi * frequency / 8000
            real = sum(value * math.cos(scale * n) for n, value in enumerate(samples))
            imag = sum(value * math.sin(scale * n) for n, value in enumerate(samples))
            return math.hypot(real, imag)

        self.assertGreater(magnitude(440), 10 * magnitude(880),
                           "The separate soundtrack must replace the clip's embedded audio")

    def test_still_image_and_long_audio_trim(self):
        output = self.render("still preview.mp4", duration="1.2")
        self.check_export(output, 1.2)
        self.check_soundtrack(output)
        # The 4:3 image should occupy x=240..1679 with black pillarboxes.
        self.assertLess(max(self.pixel(output, 0.1, 100, 540)), 20)
        self.assert_red(self.pixel(output, 0.1, 260, 540))
        self.assert_red(self.pixel(output, 1.0, 960, 540))

    def test_video_repeats_and_uses_separate_audio(self):
        output = self.render("clip preview.mp4", visual=self.clip, duration="00:00:01")
        self.check_export(output, 1.0)
        self.check_soundtrack(output)
        # Portrait 3:4 source occupies x=555..1364; both geometry and later
        # color changes establish that the short clip repeats without freezing.
        self.assertLess(max(self.pixel(output, 0.1, 500, 540)), 20)
        for time in (0.08, 0.48, 0.88):
            self.assert_red(self.pixel(output, time, 960, 540))
        for time in (0.28, 0.68):
            self.assert_blue(self.pixel(output, time, 960, 540))

    def test_jpeg_converts_full_range_without_losing_padding(self):
        output = self.render("jpeg preview.mp4", visual=self.jpeg, duration="0.2")
        self.check_export(output, 0.2)
        self.assertLess(max(self.pixel(output, 0.1, 100, 540)), 20)
        self.assert_red(self.pixel(output, 0.1, 960, 540))

    def test_short_audio_requires_explicit_loop_flag(self):
        rejected = self.directory / "short audio rejected.mp4"
        result = self.invoke(
            "--visual", self.still, "--audio", self.short_audio,
            "--duration", "1", "--output", rejected,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--loop-audio", result.stderr)
        self.assertFalse(rejected.exists())
        output = self.render("audio loop.mp4", audio=self.short_audio, extra=("--loop-audio",))
        self.check_export(output, 1.0)
        self.check_soundtrack(output, time=0.7)

    def test_existing_output_is_preserved(self):
        output = self.directory / "existing.mp4"
        original = b"previous export must survive unchanged"
        output.write_bytes(original)
        result = self.invoke(
            "--visual", self.still, "--audio", self.audio,
            "--duration", "1", "--output", output,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exist", result.stderr.lower())
        self.assertEqual(output.read_bytes(), original)

        target = self.directory / "missing symlink target.mp4"
        link = self.directory / "existing dangling symlink.mp4"
        link.symlink_to(target)
        result = self.invoke(
            "--visual", self.still, "--audio", self.audio,
            "--duration", "1", "--output", link,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exist", result.stderr.lower())
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), target)
        self.assertFalse(target.exists())

    def test_default_duration_is_one_hour(self):
        output = self.directory / "default hour.mp4"
        # The short fixture must fail preflight before any hour-long encode.
        result = self.invoke(
            "--visual", self.still, "--audio", self.audio, "--output", output,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("3600", result.stderr)
        self.assertIn("--loop-audio", result.stderr)
        self.assertFalse(output.exists())

    def test_invalid_duration_is_rejected(self):
        for value in ("0", "-1", "nan", "inf", "00:60:00", "00:00:60", "five minutes"):
            with self.subTest(duration=value):
                output = self.directory / "invalid duration.mp4"
                result = self.invoke(
                    "--visual", self.still, "--audio", self.audio,
                    "--duration", value, "--output", output,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("duration", result.stderr.lower())
                self.assertFalse(output.exists())

    def test_missing_invalid_and_wrong_stream_inputs(self):
        corrupt = self.directory / "corrupt.wav"
        corrupt.write_text("This is not media.", encoding="utf-8")
        for visual, audio in (
            (self.directory / "missing.png", self.audio),
            (self.still, self.directory / "missing.wav"),
            (self.audio, self.audio),
            (self.still, self.still),
            (self.still, corrupt),
        ):
            with self.subTest(visual=visual.name, audio=audio.name):
                output = self.directory / "bad input.mp4"
                result = self.invoke(
                    "--visual", visual, "--audio", audio,
                    "--duration", "1", "--output", output,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.strip(), "Validation should explain the failure")
                self.assertFalse(output.exists())

    def test_help(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for argument in ("--visual", "--audio", "--duration", "--output", "--loop-audio"):
            self.assertIn(argument, result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
