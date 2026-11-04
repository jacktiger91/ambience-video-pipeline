import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from PIL import Image, ImageChops, ImageDraw

from animate_water import WaterScene, load_geometry, render
from render import RenderError, probe


def photograph_fixture(size=(320, 180)):
    image = Image.new("RGB", size, (26, 32, 45))
    drawing = ImageDraw.Draw(image)
    for y in range(round(size[1]*.45), size[1], 3):
        drawing.line((0, y, size[0], y), fill=(44+y % 30, 60+y % 25, 75+y % 35))
    return image


class WaterAnimationTests(unittest.TestCase):
    def test_land_and_horizon_stay_fixed_while_water_moves(self):
        source = photograph_fixture()
        scene = WaterScene(source, size=source.size)
        frame = scene.frame(2.1)
        changed = ImageChops.difference(source, frame)
        self.assertIsNotNone(changed.getbbox())
        outside = ImageChops.invert(scene.mask)
        self.assertIsNone(ImageChops.multiply(changed, Image.merge("RGB", (outside,)*3)).getbbox())
        self.assertIsNone(changed.crop((0, 0, 320, 70)).getbbox())

    def test_cycle_seam_is_continuous_and_motion_remains_local(self):
        image = photograph_fixture()
        scene = WaterScene(image, size=image.size)
        self.assertIsNone(ImageChops.difference(scene.frame(.15), scene.frame(120.15)).getbbox())
        before = scene.displacements(119.999)
        after = scene.displacements(.001)
        self.assertLess(max(math.dist(a, b) for a, b in zip(before, after)), .01)
        points = scene.displacements(4.5)
        maximum = max(math.dist(point, (v["x"]-scene.box[0], v["y"]-scene.box[1])) for point, v in zip(points, scene.vertices))
        self.assertLess(maximum, 7 * scene.scale)

    def test_malformed_geometry_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "geometry.json"
            for bad in ({"horizon": 1}, {"water_polygon": [[0, 0], [2, 0], [1, 1]]}, {"shoreline": [[.5, .5]]}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(RenderError):
                    load_geometry(path)

    def test_twelve_second_export_and_immutable_proof(self):
        with tempfile.TemporaryDirectory(prefix="water-animation-test-") as directory:
            root = Path(directory)
            image, audio, output = root/"scene.png", root/"test-only.wav", root/"preview.mp4"
            photograph_fixture((640, 360)).save(image)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-ac", "2", str(audio)], check=True)
            proof = render(image, audio, output, duration=12, cycle=2, preset="ultrafast")
            self.assertTrue(proof["verification"]["full_decode_passed"])
            self.assertAlmostEqual(proof["verification"]["decoded_seconds"], 12, delta=.15)
            info = probe(output)
            self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]), (1920, 1080))
            self.assertEqual(info["streams"][1]["channels"], 2)
            stamp = output.stat().st_mtime_ns
            render(image, audio, output, duration=12, cycle=2, preset="ultrafast")
            self.assertEqual(output.stat().st_mtime_ns, stamp)
            with self.assertRaises(RenderError):
                render(image, audio, output, duration=12, cycle=2, seed=11, preset="ultrafast")
            with self.assertRaises(RenderError):
                render(image, audio, root/"too-long.mp4", duration=30, cycle=2)


if __name__ == "__main__":
    unittest.main()
