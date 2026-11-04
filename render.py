#!/usr/bin/env python3
"""Render a local visual and a finished soundtrack into a verified ambience MP4."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any


FPS = 30
DURATION_TOLERANCE = 0.1  # Video frames and AAC packets have discrete durations.
IMAGE_DEMUXERS = {
    "image2", "bmp_pipe", "dds_pipe", "dpx_pipe", "exr_pipe", "jpeg_pipe",
    "jpegls_pipe", "jpegxl_pipe", "pam_pipe", "pbm_pipe", "pcx_pipe",
    "pfm_pipe", "pgm_pipe", "pgmyuv_pipe", "png_pipe", "ppm_pipe",
    "qdraw_pipe", "qoi_pipe", "sgi_pipe", "sunrast_pipe", "tiff_pipe",
    "webp_pipe", "xbm_pipe", "xpm_pipe", "xwd_pipe",
}


class RenderError(Exception):
    """A user-facing input, encoding, or verification error."""


def parse_duration(value: str) -> float:
    """Accept seconds or hours:minutes:seconds, optionally with fractional seconds."""
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        seconds = float(value)
    elif re.fullmatch(r"[0-9]+:[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?", value):
        hours, minutes, seconds_text = value.split(":")
        seconds = float(hours) * 3600 + float(minutes) * 60 + float(seconds_text)
    else:
        raise argparse.ArgumentTypeError(
            "duration must be seconds (e.g. 30 or 1.5) or HH:MM:SS (e.g. 01:00:00)"
        )
    if not math.isfinite(seconds) or seconds < 1 / FPS:
        raise argparse.ArgumentTypeError("duration must be finite and at least 1/30 second")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Render an image or repeating video with a separate soundtrack to a "
            "1920x1080 H.264/stereo AAC MP4. Existing output files are never overwritten."
        )
    )
    parser.add_argument("--visual", required=True, type=Path, help="local still image or video clip")
    parser.add_argument("--audio", required=True, type=Path, help="local finished ambience soundtrack")
    parser.add_argument(
        "--duration", type=parse_duration, default=3600.0, metavar="SECONDS|HH:MM:SS",
        help="export length; fractional seconds accepted (default: 3600, one hour)",
    )
    parser.add_argument("--output", required=True, type=Path, help="new .mp4 output path")
    parser.add_argument(
        "--loop-audio", action="store_true",
        help="explicitly allow repeating a soundtrack shorter than the export",
    )
    return parser


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RenderError(f"Missing required tools: {', '.join(missing)}. See README.md for setup.")


def input_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RenderError(f"{label} must be an existing regular file: {path}")
    if path.stat().st_size == 0:
        raise RenderError(f"{label} is empty: {path}")
    if not os.access(path, os.R_OK):
        raise RenderError(f"{label} is not readable: {path}")
    return path


def probe(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"FFprobe timed out while reading {path}") from exc
    if result.returncode:
        detail = result.stderr.strip()[-2000:]
        raise RenderError(f"FFprobe could not read {path}: {detail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"FFprobe returned invalid metadata for {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
        raise RenderError(f"FFprobe found no media streams in {path}")
    return data


def stream_of_type(metadata: dict[str, Any], kind: str, label: str) -> dict[str, Any]:
    for stream in metadata["streams"]:
        if stream.get("codec_type") == kind and not stream.get("disposition", {}).get("attached_pic"):
            return stream
    raise RenderError(f"{label} has no usable {kind} stream.")


def positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def stream_duration(stream: dict[str, Any]) -> float | None:
    duration = positive_number(stream.get("duration"))
    if duration is not None:
        return duration
    # Some demuxers expose duration in time-base units instead of seconds.
    try:
        numerator, denominator = stream["time_base"].split("/")
        return positive_number(float(stream["duration_ts"]) * int(numerator) / int(denominator))
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return None


def audio_duration(metadata: dict[str, Any], stream: dict[str, Any]) -> float:
    duration = stream_duration(stream)
    if duration is None:
        # A container duration is a safe estimate only for a sole audio stream
        # (cover art is allowed), not for a longer video or another audio track.
        timed_streams = [s for s in metadata["streams"] if not s.get("disposition", {}).get("attached_pic")]
        if len(timed_streams) == 1:
            duration = positive_number(metadata.get("format", {}).get("duration"))
    if duration is None:
        raise RenderError("Cannot determine soundtrack duration. Convert it to a standard WAV or FLAC file first.")
    return duration


def verify_export(path: Path, expected_duration: float) -> dict[str, Any]:
    """Reject incomplete or mismatched encodes before exposing the requested output."""
    metadata = probe(path)
    streams = metadata["streams"]
    video = stream_of_type(metadata, "video", "Export")
    audio = stream_of_type(metadata, "audio", "Export")
    checks = {
        "exactly one video and one audio stream": len(streams) == 2,
        "H.264 video": video.get("codec_name") == "h264",
        "1920x1080 resolution": (video.get("width"), video.get("height")) == (1920, 1080),
        "yuv420p pixel format": video.get("pix_fmt") == "yuv420p",
        "square pixels": video.get("sample_aspect_ratio") == "1:1",
        "AAC audio": audio.get("codec_name") == "aac",
        "stereo audio": audio.get("channels") == 2 and audio.get("channel_layout") == "stereo",
        "48 kHz audio": str(audio.get("sample_rate")) == "48000",
        "MP4 container": "mp4" in metadata.get("format", {}).get("format_name", "").split(","),
    }
    for label, actual in (
        ("container", positive_number(metadata.get("format", {}).get("duration"))),
        ("video", stream_duration(video)),
        ("audio", stream_duration(audio)),
    ):
        checks[f"{label} duration within {DURATION_TOLERANCE}s of {expected_duration:g}s (got {actual})"] = (
            actual is not None and abs(actual - expected_duration) <= DURATION_TOLERANCE
        )
    failed = [description for description, passed in checks.items() if not passed]
    if failed:
        raise RenderError("Export verification failed: " + "; ".join(failed))
    return metadata


def render(args: argparse.Namespace) -> Path:
    require_tools()
    visual = input_file(args.visual, "Visual")
    audio_path = input_file(args.audio, "Audio")
    output = args.output.expanduser().absolute()
    if output.suffix.lower() != ".mp4":
        raise RenderError("Output filename must end in .mp4.")
    # lexists also catches dangling symlinks; never follow or replace them.
    if os.path.lexists(output):
        raise RenderError(f"Output already exists; choose a new filename: {output}")
    visual_metadata = probe(visual)
    video_stream = stream_of_type(visual_metadata, "video", "Visual")
    audio_metadata = probe(audio_path)
    audio_stream = stream_of_type(audio_metadata, "audio", "Audio")
    soundtrack_duration = audio_duration(audio_metadata, audio_stream)
    short_audio = soundtrack_duration + 0.001 < args.duration
    if short_audio and not args.loop_audio:
        raise RenderError(
            f"Soundtrack is too short ({soundtrack_duration:.3f}s) for {args.duration:g}s. "
            "Use a longer soundtrack, shorten --duration, or explicitly allow repetition with --loop-audio."
        )
    demuxers = set(visual_metadata.get("format", {}).get("format_name", "").split(","))
    is_image = bool(demuxers & IMAGE_DEMUXERS)
    threads = str(min(4, os.cpu_count() or 1))
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-stats", "-y"]
    if is_image:
        command += ["-loop", "1", "-framerate", str(FPS)]
    else:
        command += ["-stream_loop", "-1"]
    command += ["-threads", threads, "-i", str(visual)]
    if short_audio and args.loop_audio:
        command += ["-stream_loop", "-1"]
    command += ["-threads", threads, "-i", str(audio_path)]
    # 'dar' accounts for non-square source pixels. Round both scaled dimensions
    # down to even numbers for H.264, then add black padding without cropping.
    video_filter = (
        "setpts=PTS-STARTPTS,"
        "scale=w='max(2,trunc(min(1920,1080*dar)/2)*2)':"
        "h='max(2,trunc(min(1080,1920/dar)/2)*2)':out_range=tv,"
        "setsar=1,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={FPS},format=yuv420p"
    )
    command += [
        "-map", f"0:{video_stream['index']}", "-map", f"1:{audio_stream['index']}",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", video_filter, "-af", "asetpts=PTS-STARTPTS",
        "-filter_threads", threads,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-threads", threads,
        "-pix_fmt", "yuv420p", "-color_range", "tv",
    ]
    if is_image:
        command += ["-tune", "stillimage"]
    command += [
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
        "-t", f"{args.duration:.6f}", "-movflags", "+faststart", "-f", "mp4",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".tmp.mp4", dir=output.parent)
    temporary = Path(temporary_name)
    os.close(descriptor)
    try:
        print(
            f"Rendering {args.duration:g}s from {'still image' if is_image else 'repeating video'}; "
            f"audio {'will repeat (--loop-audio)' if short_audio else 'will be trimmed to length'}.",
            file=sys.stderr,
        )
        result = subprocess.run(command + [str(temporary)])
        if result.returncode:
            raise RenderError(f"FFmpeg failed with exit code {result.returncode}; see its diagnostic output above.")
        verify_export(temporary, args.duration)
        # An atomic, no-clobber publication: unlike rename/replace, link fails if
        # another process created output while encoding. Both paths share a FS.
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise RenderError(f"Output appeared during rendering; it was not overwritten: {output}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = render(args)
    except KeyboardInterrupt:
        print("\nRender cancelled; temporary output removed.", file=sys.stderr)
        return 130
    except (RenderError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Verified export: {output} (1920x1080 H.264, stereo AAC, {args.duration:g}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
