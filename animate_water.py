#!/usr/bin/env python3
"""Animate a fixed photographic shoreline without generating or looping audio.

Only the explicitly masked water is deformed. Land, buildings, sky and the
horizon remain fixed. The small perspective-scaled deformations carry the
photograph's existing wave detail and reflections; no drawn foam is added.
Requires Python 3.11+, Pillow, FFmpeg and FFprobe.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from animate_rain import atomic_json, encode_visual, file_hash, run_ffmpeg, verify_full_decode
from render import RenderError, audio_duration, parse_duration, probe, require_tools, stream_of_type, verify_export


VERSION = 1
DEFAULT_GEOMETRY = {
    "water_polygon": [[.35, .45], [1, .45], [1, 1], [.55, 1], [.40, .75]],
    "shoreline": [[.35, .45], [.40, .75], [.55, 1]],
    "horizon": .45,
    "exclusions": [],
}


def load_geometry(path: Path | None) -> dict:
    data = json.loads(path.read_text()) if path else DEFAULT_GEOMETRY
    if not isinstance(data, dict) or set(data) - set(DEFAULT_GEOMETRY):
        raise RenderError("Water geometry needs water_polygon, shoreline, horizon and optional exclusions.")
    result = {**DEFAULT_GEOMETRY, **data}
    shapes = [(result["water_polygon"], 3), (result["shoreline"], 2)]
    if not isinstance(result["exclusions"], list):
        raise RenderError("Water exclusions must be a list of normalized polygons.")
    shapes.extend((polygon, 3) for polygon in result["exclusions"])
    for points, minimum in shapes:
        if not isinstance(points, list) or len(points) < minimum or any(not isinstance(p, list) or len(p) != 2 for p in points):
            raise RenderError("Water masks and shoreline need normalized [x, y] points.")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for p in points for v in p):
            raise RenderError("Water geometry coordinates must be finite numbers from 0 to 1.")
    if not isinstance(result["horizon"], (int, float)) or not math.isfinite(result["horizon"]) or not 0 <= result["horizon"] < 1:
        raise RenderError("Water horizon must be a finite normalized y coordinate below 1.")
    return result


class WaterScene:
    """Small local texture movements and irregular, smoothly joined shore washes."""

    def __init__(self, image: Image.Image, *, size=(1920, 1080), seed=1104, cycle=120, geometry=None):
        self.width, self.height = size
        self.cycle = cycle
        self.base = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
        self.geometry = geometry or DEFAULT_GEOMETRY
        self.scale = self.height / 1080
        self.mask = self.polygon(self.geometry["water_polygon"])
        for exclusion in self.geometry["exclusions"]:
            self.mask = ImageChops.subtract(self.mask, self.polygon(exclusion))
        # Feather inward only: a pixel outside water is never changed.
        feather = self.mask.filter(ImageFilter.GaussianBlur(max(1, 7 * self.scale)))
        self.blend_mask = ImageChops.multiply(self.mask, feather)
        self.motion_mask = self.mask.filter(ImageFilter.GaussianBlur(max(1, 18 * self.scale)))
        bounds = self.mask.getbbox()
        if bounds is None:
            raise RenderError("The water mask is empty.")
        left, top, right, bottom = bounds
        self.box = (max(0, left - 12), max(0, top - 12), min(self.width, right + 12), min(self.height, bottom + 12))
        self.crop = self.base.crop(self.box)
        self.crop_mask = self.blend_mask.crop(self.box)
        rng = random.Random(seed)
        # Several spatial scales and unrelated travel periods avoid a single
        # uniform sinusoidal 'pond' distortion. Each closes over the long cycle.
        self.waves = []
        for amplitude, wavelength, period in [(1.00, 155, 5.7), (.67, 83, 8.3), (.40, 41, 3.7), (.26, 230, 13.1), (.19, 27, 4.4)]:
            self.waves.append({"amplitude": amplitude, "kx": rng.uniform(-.025, .025) / self.scale,
                               "ky": 2 * math.pi / (wavelength * self.scale),
                               "turns": max(1, round(cycle / period)), "phase": rng.uniform(0, 2 * math.pi)})
        self.washes = []
        onset = 0.0
        while onset < cycle:
            self.washes.append({"onset": onset, "life": min(cycle, rng.uniform(5.2, 9.7)),
                                "strength": rng.uniform(.45, 1), "along": rng.uniform(0, 2 * math.pi)})
            onset += rng.uniform(5.0, 12.5)
        self.shore = [(x * self.width, y * self.height) for x, y in self.geometry["shoreline"]]
        step_x, step_y = max(6, round(44 * self.scale)), max(4, round(16 * self.scale))
        xs = list(range(self.box[0], self.box[2], step_x)) + [self.box[2]]
        ys = list(range(self.box[1], self.box[3], step_y)) + [self.box[3]]
        self.vertices = [self.vertex(x, y) for y in ys for x in xs]
        self.quads = []
        for row in range(len(ys) - 1):
            for column in range(len(xs) - 1):
                a = row * len(xs) + column
                box = (xs[column] - self.box[0], ys[row] - self.box[1], xs[column+1] - self.box[0], ys[row+1] - self.box[1])
                self.quads.append((box, (a, a + len(xs), a + len(xs) + 1, a + 1)))

    def polygon(self, points):
        mask = Image.new("L", self.base.size, 0)
        ImageDraw.Draw(mask).polygon([(round(x*self.width), round(y*self.height)) for x, y in points], fill=255)
        return mask

    def vertex(self, x, y):
        depth = max(0, min(1, (y / self.height - self.geometry["horizon"]) / (1 - self.geometry["horizon"])))
        distance, normal_x, normal_y = float("inf"), 0., 0.
        for (ax, ay), (bx, by) in zip(self.shore, self.shore[1:]):
            length = (bx-ax)**2 + (by-ay)**2
            if not length:
                continue
            position = max(0, min(1, ((x-ax)*(bx-ax) + (y-ay)*(by-ay)) / length))
            dx, dy = x - (ax + position*(bx-ax)), y - (ay + position*(by-ay))
            candidate = math.hypot(dx, dy)
            if candidate < distance:
                distance = candidate
                normal_x, normal_y = (dx/candidate, dy/candidate) if candidate > .01 else (0., 0.)
        mask_position = (min(self.width-1, max(0, x)), min(self.height-1, max(0, y)))
        edge = max(0, min(1, (self.motion_mask.getpixel(mask_position) / 255 - .5) * 2)) ** .65
        edge *= self.mask.getpixel(mask_position) / 255
        shore_weight = math.exp(-(distance / max(1, (19 + depth * 55) * self.scale)) ** 2)
        waves = []
        for wave in self.waves:
            angle = wave["kx"] * x + wave["ky"] * y + wave["phase"]
            waves.append((math.sin(angle), math.cos(angle)))
        return {"x": x, "y": y, "depth": depth, "edge": edge, "shore_weight": shore_weight,
                "normal_x": normal_x, "normal_y": normal_y, "waves": waves}

    def displacements(self, time):
        phase = (time % self.cycle) / self.cycle
        rotations = [(math.cos(2*math.pi*phase*w["turns"]), math.sin(2*math.pi*phase*w["turns"])) for w in self.waves]
        active_washes = []
        for wash in self.washes:
            age = (time - wash["onset"]) % self.cycle
            if age < wash["life"]:
                progress = age / wash["life"]
                # Quick approach, slower retreat, both with zero endpoint slope.
                up = min(1, progress / .38)
                down = min(1, (1-progress) / .62)
                envelope = .5 - .5*math.cos(math.pi * min(up, down))
                active_washes.append((wash, envelope))
        points = []
        for vertex in self.vertices:
            value, sideways = 0., 0.
            for wave, spatial, rotation in zip(self.waves, vertex["waves"], rotations):
                value += wave["amplitude"] * (spatial[0]*rotation[0] - spatial[1]*rotation[1])
                sideways += wave["amplitude"] * (spatial[1]*rotation[0] + spatial[0]*rotation[1])
            depth = vertex["depth"] ** 1.35
            dy = value * depth * self.scale
            dx = sideways * depth * .28 * self.scale
            for wash, envelope in active_washes:
                along = .78 + .22 * math.sin(vertex["x"]/(185*self.scale) + wash["along"])
                amount = 3.1 * self.scale * wash["strength"] * envelope * along * vertex["shore_weight"] * depth
                dx += vertex["normal_x"] * amount
                dy += vertex["normal_y"] * amount
            points.append((max(0, min(self.crop.width, vertex["x"] - self.box[0] + dx*vertex["edge"])),
                           max(0, min(self.crop.height, vertex["y"] - self.box[1] + dy*vertex["edge"]))))
        return points

    def frame(self, time, cues=()):
        points = self.displacements(time)
        mesh = [(box, tuple(value for index in indexes for value in points[index])) for box, indexes in self.quads]
        water = self.crop.transform(self.crop.size, Image.Transform.MESH, mesh, resample=Image.Resampling.BICUBIC)
        frame = self.base.copy()
        frame.paste(water, self.box[:2], self.crop_mask)
        return frame


def render(scene_path: Path, audio_path: Path, output: Path, *, duration=3600., seed=1104, cycle=120, geometry=None, preset="fast"):
    require_tools()
    scene_path, audio_path, output = scene_path.resolve(), audio_path.resolve(), output.absolute()
    if not scene_path.is_file() or not audio_path.is_file():
        raise RenderError("Scene and verified continuous soundtrack must exist.")
    if output.suffix.lower() != ".mp4" or cycle < 2 or cycle % 2:
        raise RenderError("Output must end in .mp4; cycle must be an even integer of at least 2 seconds.")
    audio_info = probe(audio_path)
    audio_stream = stream_of_type(audio_info, "audio", "Soundtrack")
    if audio_stream.get("channels") != 2:
        raise RenderError("Shore field recording must be stereo; mono is not silently widened.")
    if audio_duration(audio_info, audio_stream) + .001 < duration:
        raise RenderError("The continuous soundtrack is too short. Water animation never loops audio.")
    geometry = geometry or DEFAULT_GEOMETRY
    recipe = {"animation": "photographic_water", "version": VERSION, "scene_sha256": file_hash(scene_path),
              "audio_sha256": file_hash(audio_path), "duration": duration, "seed": seed,
              "cycle": cycle, "geometry": geometry, "preset": preset}
    proof_path = output.with_suffix(".provenance.json")
    if os.path.lexists(output):
        if proof_path.is_file():
            proof = json.loads(proof_path.read_text())
            if proof.get("recipe") == recipe and proof.get("verification", {}).get("full_decode_passed") is True and proof["verification"].get("sha256") == file_hash(output):
                print("Reusing verified water animation: " + str(output), flush=True)
                return proof
        raise RenderError("Existing output does not match this verified recipe; choose a new filename.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(scene_path) as photo:
        scene = WaterScene(photo, seed=seed, cycle=cycle, geometry=geometry)
    with tempfile.TemporaryDirectory(prefix=".water-animation-", dir=output.parent) as name:
        temp = Path(name)
        base, encoded = temp / "water.mp4", temp / "complete.mp4"
        visual_duration = min(float(cycle), math.ceil(duration / 2) * 2)
        print(f"Encoding {visual_duration:g}s of masked photographic water motion...", flush=True)
        encode_visual(scene, base, 0, visual_duration, preset=preset)
        run_ffmpeg(["-stream_loop", "-1", "-i", str(base), "-i", str(audio_path),
                    "-map", "0:v:0", "-map", f"1:{audio_stream['index']}", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-ac", "2", "-ar", "48000", "-threads", "4", "-t", f"{duration:.6f}",
                    "-map_metadata", "-1", "-movflags", "+faststart", str(encoded)])
        metadata = verify_export(encoded, duration)
        print("Checking every frame and audio packet through the end...", flush=True)
        decoded = verify_full_decode(encoded, duration)
        proof = {"schema_version": 1, "recipe": recipe,
                 "visual_method": f"Fixed photographic coast; local masked water/reflection warps and irregular shore wash, seamless {cycle}s visual cycle. Audio never loops.",
                 "verification": {"full_decode_passed": True, "decoded_seconds": decoded,
                                  "sha256": file_hash(encoded), "duration": float(metadata["format"]["duration"])}}
        os.link(encoded, output)
        atomic_json(proof_path, proof)
    print("Verified photographic water animation: " + str(output), flush=True)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=parse_duration, default=3600.)
    parser.add_argument("--seed", type=int, default=1104)
    parser.add_argument("--cycle", type=int, default=120)
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--preset", choices=("ultrafast", "veryfast", "fast", "medium"), default="fast")
    args = parser.parse_args()
    try:
        render(args.scene, args.audio, args.output, duration=args.duration, seed=args.seed, cycle=args.cycle,
               geometry=load_geometry(args.geometry), preset=args.preset)
    except KeyboardInterrupt:
        print("Water animation cancelled; earlier exports remain unchanged.", file=sys.stderr)
        return 130
    except (RenderError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
