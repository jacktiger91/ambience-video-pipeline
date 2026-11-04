#!/usr/bin/env python3
"""Animate a photographic shelter scene and mux a continuous, verified soundtrack.

Requires Python 3.11+, Pillow, FFmpeg and FFprobe. The underlying photograph is
never regenerated here. Rain/splash video frames are composited over it, with
explicit masks keeping the shelter dry. A 90-second seamless visual cycle is
encoded once; the soundtrack is never looped. Optional thunder glow patches are
encoded only around supplied rumble onsets, keeping long exports CPU-efficient.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from render import RenderError, audio_duration, parse_duration, probe, require_tools, stream_of_type, verify_export


FPS = 30
SEGMENT_SECONDS = 2
VERSION = 3
DEFAULT_GEOMETRY = {
    "rain_polygon": [[.268, .115], [.909, .115], [.909, .837], [.268, .837]],
    "ground_polygon": [[.268, .614], [.909, .614], [.909, .835], [.268, .835]],
    "sky_polygon": [[.50, .118], [.78, .118], [.80, .20], [.81, .30], [.70, .34], [.56, .31], [.50, .20]],
    "exclusions": [],
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".rain-proof-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_geometry(path: Path | None) -> dict:
    data = json.loads(path.read_text()) if path else DEFAULT_GEOMETRY
    if not isinstance(data, dict) or set(data) - set(DEFAULT_GEOMETRY):
        raise RenderError("Geometry needs rain_polygon, ground_polygon, sky_polygon and optional exclusions.")
    result = {**DEFAULT_GEOMETRY, **data}
    for polygon in [result[name] for name in ("rain_polygon", "ground_polygon", "sky_polygon")] + result["exclusions"]:
        if len(polygon) < 3 or any(len(point) != 2 for point in polygon):
            raise RenderError("Each mask must be a polygon with at least three normalized points.")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for point in polygon for v in point):
            raise RenderError("Mask coordinates must be finite numbers from 0 to 1.")
    return result


def load_cues(path: Path | None, duration: float) -> list[dict]:
    data = json.loads(path.read_text()) if path else []
    if not isinstance(data, list):
        raise RenderError("Thunder cues must be a JSON list of rumble-onset seconds or {time, strength} objects.")
    result = []
    for item in data:
        cue = {"time": item, "strength": .075} if isinstance(item, (int, float)) else item
        if not isinstance(cue, dict) or set(cue) - {"time", "strength"}:
            raise RenderError("Invalid thunder cue.")
        when, strength = cue.get("time"), cue.get("strength", .075)
        if not isinstance(when, (int, float)) or not math.isfinite(when) or not 0 <= when < duration:
            raise RenderError("Thunder onset must fall inside the export.")
        if not isinstance(strength, (int, float)) or not math.isfinite(strength) or not 0 <= strength <= .15:
            raise RenderError("Thunder glow strength must be from 0 to 0.15 to avoid harsh flashes.")
        result.append({"time": float(when), "strength": float(strength)})
    return sorted(result, key=lambda cue: cue["time"])


class RainScene:
    """Deterministic, periodic motion with perspective, depth and shelter masks."""

    def __init__(self, image: Image.Image, *, size=(1920, 1080), seed=7304, cycle=90, geometry=None):
        self.width, self.height = size
        self.cycle = cycle
        self.base = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
        self.geometry = geometry or DEFAULT_GEOMETRY
        self.scale = self.height / 1080
        self.rain_mask = self.mask(self.geometry["rain_polygon"])
        self.ground_mask = self.mask(self.geometry["ground_polygon"])
        self.sky_mask = self.mask(self.geometry["sky_polygon"]).filter(ImageFilter.GaussianBlur(35 * self.scale))
        for polygon in self.geometry["exclusions"]:
            exclusion = self.mask(polygon)
            self.rain_mask = ImageChops.subtract(self.rain_mask, exclusion)
            self.ground_mask = ImageChops.subtract(self.ground_mask, exclusion)
            self.sky_mask = ImageChops.subtract(self.sky_mask, exclusion)
        rng = random.Random(seed)
        self.drops = []
        # Each fall period divides the visual cycle exactly. Loop boundaries
        # therefore advance motion by one ordinary frame instead of jumping.
        for count, speed, length, width, alpha in (
            (520, (100, 245), (2, 6), 1, (11, 21)),
            (190, (430, 760), (7, 16), 1, (15, 27)),
            (34, (1000, 1550), (22, 38), 2, (11, 21)),
        ):
            for _ in range(count):
                requested_speed = rng.uniform(*speed) * self.scale
                distance = self.height * 1.12
                turns = max(1, round(cycle * requested_speed / distance))
                self.drops.append({
                    "x": rng.uniform(-.03, 1.03) * self.width,
                    "phase": rng.random(), "turns": turns,
                    "length": rng.uniform(*length) * self.scale,
                    "width": max(1, round(width * self.scale)),
                    "alpha": rng.randint(*alpha), "lean": rng.uniform(.06, .14),
                })
        self.splashes = []
        for _ in range(150):
            x = rng.uniform(.16, .96) * self.width
            y = (.61 + .32 * rng.random() ** 1.6) * self.height
            depth = max(0, (y / self.height - .58) / .4)
            self.splashes.append({
                "x": x, "y": y, "phase": rng.random(),
                "turns": max(1, round(cycle / rng.uniform(3.5, 8.0))),
                "radius": (2.5 + 8.5 * depth ** 1.4) * self.scale,
                "life": rng.uniform(.38, .73), "alpha": rng.randint(18, 34),
                "height": (1 + 5 * depth) * self.scale,
                "flight": rng.uniform(.10, .15),
                "travel": (45 + 75 * depth) * self.scale,
            })

    def mask(self, polygon) -> Image.Image:
        mask = Image.new("L", (self.width, self.height), 0)
        ImageDraw.Draw(mask).polygon([(round(x * self.width), round(y * self.height)) for x, y in polygon], fill=255)
        return mask

    def light_color(self, x, y):
        # Warm spill only near the left lantern/low wet courtyard; the distance
        # remains cool and restrained, matching the photographic base lighting.
        warm = max(0, 1 - abs(x / self.width - .24) / .25) * max(0, (y / self.height - .5) / .5)
        return tuple(round(cool * (1 - warm) + lit * warm) for cool, lit in zip((157, 184, 205), (217, 172, 100)))

    @staticmethod
    def glow_level(time, cues):
        level = 0.0
        for cue in cues:
            offset = time - (cue["time"] - .35)
            if -.17 <= offset <= .8:
                pulse = math.exp(-((offset / (.08 if offset < 0 else .23)) ** 2))
                level = max(level, cue["strength"] * pulse)
        return level

    def impact_state(self, splash, time):
        """Tie an incoming close drop to the exact origin of its water impact."""
        period = self.cycle / splash["turns"]
        age = ((splash["phase"] + (time % self.cycle) / period) % 1) * period
        remaining = period - age
        tip = None
        if remaining <= splash["flight"]:
            distance = splash["travel"] * remaining / splash["flight"]
            tip = (splash["x"] + distance * .10, splash["y"] - distance)
        elif age < 1 / FPS:
            # The first impact frame still carries the arriving tip. Its tail
            # fades as the droplets/ring begin at exactly the same coordinate.
            tip = (splash["x"], splash["y"])
        return age, tip

    def frame(self, time: float, cues=()) -> Image.Image:
        frame = self.base.copy()
        rain = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(rain)
        phase = (time % self.cycle) / self.cycle
        drift = 1 + .11 * math.sin(2 * math.pi * phase) + .05 * math.sin(6 * math.pi * phase)
        for drop in self.drops:
            fall = (drop["phase"] + phase * drop["turns"]) % 1
            y = fall * self.height * 1.12 - self.height * .06
            x = drop["x"] - fall * self.height * drop["lean"]
            length = drop["length"]
            color = self.light_color(x, y)
            alpha = round(drop["alpha"] * drift)
            draw.line((x, y, x + length * drop["lean"], y - length), fill=(*color, alpha), width=drop["width"])
        for splash in self.splashes:
            age, tip = self.impact_state(splash, time)
            if tip is None:
                continue
            x, y = tip
            tail = max(2, splash["travel"] * .10)
            alpha = splash["alpha"] * (.75 if age > splash["life"] else max(0, 1 - age * FPS))
            draw.line((x, y, x + tail * .10, y - tail), fill=(*self.light_color(x, y), round(alpha)), width=1)
        rain_alpha = ImageChops.multiply(rain.getchannel("A"), self.rain_mask)
        frame.paste(rain, (0, 0), rain_alpha)

        water = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(water)
        for splash in self.splashes:
            age, _ = self.impact_state(splash, time)
            if age >= splash["life"]:
                continue
            progress = age / splash["life"]
            radius = splash["radius"] * (.2 + .8 * progress)
            alpha = round(splash["alpha"] * (1 - progress) ** 1.4)
            x, y = splash["x"], splash["y"]
            color = self.light_color(x, y)
            draw.ellipse((x-radius, y-radius*.24, x+radius, y+radius*.24), outline=(*color, alpha), width=1)
            if progress < .24:
                height = splash["height"] * math.sin(math.pi * progress / .24)
                for side in (-1, 1):
                    dx = side * radius * .75
                    draw.line((x, y, x+dx, y-height), fill=(*color, round(alpha*.65)), width=1)
        water_alpha = ImageChops.multiply(water.getchannel("A"), self.ground_mask)
        frame.paste(water, (0, 0), water_alpha)
        glow = self.glow_level(time, cues)
        if glow:
            alpha = self.sky_mask.point(lambda value: round(value * glow))
            frame.paste(Image.new("RGB", frame.size, (200, 219, 236)), (0, 0), alpha)
        return frame


def run_ffmpeg(arguments: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *arguments], check=True)


def verify_full_decode(path: Path, duration: float) -> float:
    """Require both successful decoding and evidence that decoding reached EOF."""
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
        "-threads", "4", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0",
        "-progress", "pipe:1", "-nostats", "-f", "null", "-",
    ], capture_output=True, text=True, check=True, timeout=max(120, duration * 2))
    times = [int(line.split("=", 1)[1]) / 1_000_000 for line in result.stdout.splitlines()
             if line.startswith("out_time_us=") and line.split("=", 1)[1].lstrip("-").isdigit()]
    if not times or abs(max(times) - duration) > .15:
        raise RenderError("Full video/audio decode did not reach the expected end of the export.")
    return max(times)


def encode_visual(scene: RainScene, output: Path, start: float, duration: float, cues=(), *, preset="fast") -> None:
    frames = round(duration * FPS)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{scene.width}x{scene.height}", "-r", str(FPS), "-i", "pipe:0",
        "-an", "-vf", "scale=out_range=tv,setsar=1,format=yuv420p", "-filter_threads", "2",
        "-c:v", "libx264", "-crf", "21", "-preset", preset, "-threads", "4",
        "-g", str(SEGMENT_SECONDS*FPS), "-keyint_min", str(SEGMENT_SECONDS*FPS), "-sc_threshold", "0", "-bf", "0",
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-movflags", "+faststart", str(output),
    ]
    with tempfile.TemporaryFile(mode="w+b") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        try:
            assert process.stdin is not None
            for index in range(frames):
                process.stdin.write(scene.frame(start + index / FPS, cues).tobytes())
                if index and index % (15 * FPS) == 0:
                    print(f"Animated visual: {index/FPS:g}/{duration:g}s", flush=True)
            process.stdin.close()
            code = process.wait()
            if code:
                log.seek(0)
                raise RenderError("Visual encode failed: " + log.read().decode(errors="replace")[-2000:])
        except BaseException:
            if process.poll() is None:
                process.terminate()
                process.wait()
            raise


def render(scene_path: Path, audio_path: Path, output: Path, *, duration=3600.0, seed=7304,
           cycle=90, geometry=None, cues=(), preset="fast") -> dict:
    require_tools()
    scene_path, audio_path, output = scene_path.resolve(), audio_path.resolve(), output.absolute()
    if not scene_path.is_file() or not audio_path.is_file():
        raise RenderError("Scene and soundtrack must be existing files.")
    if output.suffix.lower() != ".mp4" or cycle < 2 or cycle % 2:
        raise RenderError("Output must end in .mp4 and cycle seconds must be an even integer of at least 2.")
    audio_info = probe(audio_path)
    audio_stream = stream_of_type(audio_info, "audio", "Soundtrack")
    if audio_stream.get("channels") != 2:
        raise RenderError("The real-rain soundtrack must be stereo; mono is not silently widened.")
    if audio_duration(audio_info, audio_stream) + .001 < duration:
        raise RenderError("The continuous soundtrack is too short. This renderer never loops audio.")
    geometry = geometry or DEFAULT_GEOMETRY
    recipe = {"version": VERSION, "scene_sha256": file_hash(scene_path), "audio_sha256": file_hash(audio_path),
              "duration": duration, "seed": seed, "cycle": cycle, "geometry": geometry, "thunder_cues": list(cues), "preset": preset}
    proof_path = output.with_suffix(".provenance.json")
    if os.path.lexists(output):
        if proof_path.is_file():
            proof = json.loads(proof_path.read_text())
            if proof.get("recipe") == recipe and proof.get("verification", {}).get("full_decode_passed") is True and proof["verification"].get("sha256") == file_hash(output):
                print("Reusing matching, fully verified animation: " + str(output), flush=True)
                return proof
        raise RenderError("Existing output does not match this verified recipe; choose a new filename.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(scene_path) as photo:
        scene = RainScene(photo, seed=seed, cycle=cycle, geometry=geometry)
    with tempfile.TemporaryDirectory(prefix=".rain-animation-", dir=output.parent) as name:
        temp = Path(name)
        base = temp / "base.mp4"
        base_duration = min(float(cycle), math.ceil(duration / SEGMENT_SECONDS) * SEGMENT_SECONDS)
        print(f"Encoding {base_duration:g}s of seamless visual rain; continuous audio remains unchanged.", flush=True)
        encode_visual(scene, base, 0, base_duration, preset=preset)
        video_input = ["-stream_loop", "-1", "-i", str(base)]
        if cues:
            run_ffmpeg(["-i", str(base), "-map", "0:v:0", "-c", "copy", "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1", str(temp / "base-%04d.mp4")])
            segments = sorted(temp.glob("base-*.mp4"))
            expected_segments = round(base_duration / SEGMENT_SECONDS)
            if len(segments) != expected_segments:
                raise RenderError("Unexpected base segment count; refusing an incomplete timeline.")
            playlist = []
            for index in range(math.ceil(duration / SEGMENT_SECONDS)):
                start = index * SEGMENT_SECONDS
                overlaps = any(start < cue["time"] + .45 and start + SEGMENT_SECONDS > cue["time"] - .52 for cue in cues)
                if overlaps:
                    piece = temp / f"glow-{index:04d}.mp4"
                    print(f"Encoding subtle distant glow near {start:g}s.", flush=True)
                    encode_visual(scene, piece, start, SEGMENT_SECONDS, cues, preset=preset)
                else:
                    piece = segments[index % len(segments)]
                playlist.append("file '" + piece.name + "'\n")
            listing = temp / "timeline.txt"
            listing.write_text("".join(playlist))
            video_input = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        encoded = temp / "complete.mp4"
        run_ffmpeg([
            *video_input, "-i", str(audio_path), "-map", "0:v:0", "-map", f"1:{audio_stream['index']}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000", "-threads", "4",
            "-t", f"{duration:.6f}", "-map_metadata", "-1", "-movflags", "+faststart", str(encoded),
        ])
        metadata = verify_export(encoded, duration)
        print("Checking every frame and audio packet through the end...", flush=True)
        decoded_seconds = verify_full_decode(encoded, duration)
        proof = {"schema_version": 1, "recipe": recipe,
                 "visual_method": f"Fixed photographic scene with three motion depths and perspective splashes; seamless {cycle}s visual cycle, no audio loop.",
                 "verification": {"full_decode_passed": True, "decoded_seconds": decoded_seconds,
                                  "sha256": file_hash(encoded), "duration": float(metadata['format']['duration'])}}
        os.link(encoded, output)
        atomic_json(proof_path, proof)
    print("Verified photographic rain animation: " + str(output), flush=True)
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=parse_duration, default=3600.0)
    parser.add_argument("--seed", type=int, default=7304)
    parser.add_argument("--cycle", type=int, default=90, help="even visual-cycle duration in seconds; audio is never looped")
    parser.add_argument("--geometry", type=Path, help="normalized rain/ground/sky mask polygons and shelter exclusions")
    parser.add_argument("--thunder-cues", type=Path, help="JSON rumble onset times; no cues means no sky glow")
    parser.add_argument("--preset", choices=("ultrafast", "veryfast", "fast", "medium"), default="fast")
    args = parser.parse_args()
    try:
        render(args.scene, args.audio, args.output, duration=args.duration, seed=args.seed, cycle=args.cycle,
               geometry=load_geometry(args.geometry), cues=load_cues(args.thunder_cues, args.duration), preset=args.preset)
    except KeyboardInterrupt:
        print("Animation cancelled; earlier exports remain unchanged.", file=sys.stderr)
        return 130
    except (RenderError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
