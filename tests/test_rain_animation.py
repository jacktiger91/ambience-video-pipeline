import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageChops

from animate_rain import RainScene, load_cues, load_geometry, render, verify_full_decode
from render import RenderError, probe


class RainAnimationTests(unittest.TestCase):
    def test_motion_is_seamless_and_shelter_stays_dry(self):
        image = Image.new("RGB", (320, 180), (14, 23, 35))
        scene = RainScene(image, size=image.size, cycle=10)
        self.assertIsNone(ImageChops.difference(scene.frame(.12), scene.frame(10.12)).getbbox())
        self.assertIsNotNone(ImageChops.difference(scene.frame(.12), scene.frame(.22)).getbbox())
        self.assertIsNone(ImageChops.difference(scene.frame(.12).crop((0, 0, 60, 180)), image.crop((0, 0, 60, 180))).getbbox())
        self.assertIsNone(ImageChops.difference(scene.frame(.12).crop((0, 0, 320, 12)), image.crop((0, 0, 320, 12))).getbbox())

    def test_glow_precedes_rumble_and_rejects_strong_flash(self):
        cue = [{"time": 2.0, "strength": .075}]
        self.assertAlmostEqual(RainScene.glow_level(1.65, cue), .075)
        self.assertEqual(RainScene.glow_level(1.0, cue), 0)
        self.assertLess(RainScene.glow_level(2.0, cue), .02)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cues.json'
            path.write_text(json.dumps([{"time": 2.0, "strength": .9}]))
            with self.assertRaises(RenderError):
                load_cues(path, 12)

    def test_close_drop_lands_at_its_own_ripple_origin(self):
        scene = RainScene(Image.new('RGB', (320, 180)), size=(320, 180), cycle=10)
        splash = scene.splashes[0]
        period = scene.cycle / splash['turns']
        onset = (1 - splash['phase']) * period
        before_age, before_tip = scene.impact_state(splash, onset - splash['flight'] / 2)
        self.assertGreater(before_age, splash['life'])
        self.assertIsNotNone(before_tip)
        self.assertLess(before_tip[1], splash['y'])
        near_age, near_tip = scene.impact_state(splash, onset - .000001)
        self.assertAlmostEqual(near_tip[0], splash['x'], places=3)
        self.assertAlmostEqual(near_tip[1], splash['y'], places=3)
        after_age, after_tip = scene.impact_state(splash, onset + .001)
        self.assertLess(after_age, splash['life'])
        self.assertEqual(after_tip, (splash['x'], splash['y']))

    def test_invalid_geometry_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'geometry.json'
            path.write_text(json.dumps({'rain_polygon': [[0, 0], [2, 0], [1, 1]]}))
            with self.assertRaises(RenderError):
                load_geometry(path)

    def test_successful_but_early_decode_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, stdout='out_time_us=1000000\nprogress=end\n', stderr='')
        with mock.patch('animate_rain.subprocess.run', return_value=result):
            with self.assertRaises(RenderError):
                verify_full_decode(Path('/unused/test.mp4'), 12)

    def test_real_encode_full_decode_and_immutable_cache(self):
        with tempfile.TemporaryDirectory(prefix='rain-animation-test-') as directory:
            root = Path(directory)
            image = root / 'scene.png'
            Image.new('RGB', (640, 360), (15, 25, 36)).save(image)
            audio = root / 'sound.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=12', '-ac', '2', str(audio)], check=True)
            output = root / 'preview.mp4'
            proof = render(image, audio, output, duration=12, cycle=2, cues=[{'time': 5.4, 'strength': .075}], preset='ultrafast')
            self.assertTrue(proof['verification']['full_decode_passed'])
            self.assertAlmostEqual(proof['verification']['decoded_seconds'], 12, delta=.15)
            info = probe(output)
            self.assertAlmostEqual(float(info['format']['duration']), 12, delta=.1)
            self.assertEqual([(s['codec_type'], s['codec_name']) for s in info['streams']], [('video', 'h264'), ('audio', 'aac')])
            self.assertEqual((info['streams'][0]['width'], info['streams'][0]['height']), (1920, 1080))
            original = output.stat().st_mtime_ns
            render(image, audio, output, duration=12, cycle=2, cues=[{'time': 5.4, 'strength': .075}], preset='ultrafast')
            self.assertEqual(output.stat().st_mtime_ns, original)
            with self.assertRaises(RenderError):
                render(image, audio, output, duration=12, seed=2, cycle=2, preset='ultrafast')


if __name__ == '__main__':
    unittest.main()
