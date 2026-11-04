#!/usr/bin/env python3
"""Prepare a verified hour + two Shorts review bundle without uploading anything."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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

from render import RenderError, probe, require_tools, stream_duration, stream_of_type


SCHEMA_VERSION = 1
RECIPE_VERSION = 1
INVITATION = "Watch the full hour on our channel."
CTA_SECOND_LINES = {"channel": "on our channel", "related": "Tap the related video", "quiet-hours": "on Quiet Hours"}
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".bundle-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def text_field(config: dict[str, Any], name: str, limit: int | None = None) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RenderError(f"Episode field '{name}' must contain the actual approved text.")
    value = value.strip()
    if limit and len(value.encode("utf-8")) > limit:
        raise RenderError(f"Episode field '{name}' exceeds {limit} UTF-8 bytes.")
    return value


def seconds(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise RenderError(f"{field} must be a finite number of seconds.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RenderError(f"{field} must be a finite number of seconds.") from exc
    if not math.isfinite(number):
        raise RenderError(f"{field} must be finite.")
    return number


def horizontal_crop(short: dict[str, Any]) -> float:
    """Fraction of the available horizontal crop, preserving legacy presets."""
    position = seconds(short.get("crop_position", {"left": 0, "center": 0.5, "right": 1}[short.get("framing", "center")]), "crop_position")
    if not 0 <= position <= 1:
        raise RenderError("crop_position must be between 0 and 1, inclusive.")
    return position


def validate_episode(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise RenderError("Episode JSON must be an object.")
    result = dict(config)
    result["bundle_id"] = text_field(config, "bundle_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", result["bundle_id"]):
        raise RenderError("bundle_id must be 1–80 letters, digits, underscores or hyphens.")
    channel_id = config.get("intended_channel_id")
    handle = config.get("intended_channel_handle")
    if channel_id and (not isinstance(channel_id, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id)):
        raise RenderError("intended_channel_id must be a complete YouTube channel ID beginning UC.")
    if not channel_id and (not isinstance(handle, str) or not re.fullmatch(r"@[A-Za-z0-9_.-]{3,30}", handle)):
        raise RenderError("Set intended_channel_id or intended_channel_handle; authentication must verify the resolved channel before upload.")
    result["intended_channel_id"] = channel_id or None
    result["intended_channel_handle"] = handle or None
    for field, limit in (("theme", None), ("title", 100), ("description", 5000), ("recording_credits", 4000)):
        result[field] = text_field(config, field, limit)
    for field in ("made_for_kids", "contains_synthetic_media"):
        if type(config.get(field)) is not bool:
            raise RenderError(f"Set {field} explicitly to true or false after reviewing the episode.")
    if config.get("playlist_id") is not None and not isinstance(config["playlist_id"], str):
        raise RenderError("playlist_id must be a string when supplied.")
    if "thumbnail_path" in config:
        result["thumbnail_path"] = text_field(config, "thumbnail_path")
    shorts = config.get("shorts")
    if not isinstance(shorts, list) or len(shorts) != 2:
        raise RenderError("Episode must define exactly two Shorts.")
    normalized = []
    for index, short in enumerate(shorts, 1):
        if not isinstance(short, dict):
            raise RenderError(f"Short {index} must be an object.")
        item = dict(short)
        item["title"] = text_field(short, "title", 100)
        item["description"] = text_field(short, "description", 5000)
        if "recording_credits" in short:
            item["recording_credits"] = text_field(short, "recording_credits", 4000)
        item["start"] = seconds(short.get("start"), f"Short {index} start")
        item["duration"] = seconds(short.get("duration"), f"Short {index} duration")
        if not 15 <= item["duration"] <= 30:
            raise RenderError(f"Short {index} duration must be 15–30 seconds.")
        if item["start"] < 0 or item["start"] + item["duration"] > 3600:
            raise RenderError(f"Short {index} must lie inside the one-hour source.")
        item["framing"] = short.get("framing", "center")
        if item["framing"] not in {"left", "center", "right"}:
            raise RenderError(f"Short {index} framing must be left, center or right.")
        if "crop_position" in short:
            item["crop_position"] = horizontal_crop(item)
        item["cta"] = short.get("cta", "channel")
        if item["cta"] not in CTA_SECOND_LINES:
            raise RenderError(f"Short {index} cta must be channel, related or quiet-hours.")
        item["cover_offset"] = seconds(short.get("cover_offset", item["duration"] - 3), f"Short {index} cover_offset")
        if not 0 <= item["cover_offset"] < item["duration"]:
            raise RenderError(f"Short {index} cover_offset must be inside its duration.")
        normalized.append(item)
    if (normalized[0]["start"], horizontal_crop(normalized[0])) == (normalized[1]["start"], horizontal_crop(normalized[1])):
        raise RenderError("The Shorts must use different moments or different framing.")
    if normalized[0]["title"] == normalized[1]["title"]:
        raise RenderError("Give each Short its own distinct title.")
    result["shorts"] = normalized
    # Validate composed descriptions before an expensive render begins.
    for entry in (result, *normalized):
        description(entry["description"], entry.get("recording_credits", result["recording_credits"]), entry is not result)
    return result


def description(body: str, credits: str, short: bool = False) -> str:
    text = body.strip() + (f"\n\n{INVITATION}" if short else "") + f"\n\nRecording credits:\n{credits.strip()}"
    if len(text.encode("utf-8")) > 5000:
        raise RenderError("Description plus recording credits exceeds YouTube's 5,000-byte limit.")
    return text


def run_ffmpeg(arguments: list[str], *, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-filter_threads", "2", *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RenderError("FFmpeg exceeded its allowed processing time.") from exc
    if result.returncode:
        raise RenderError("FFmpeg failed: " + result.stderr.strip()[-3000:])
    return result


def verify_media(path: Path, width: int, height: int, duration: float,
                 prior_verification: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe every stream and decode every packet through the end of the export."""
    metadata = probe(path)
    video = stream_of_type(metadata, "video", "Export")
    audio = stream_of_type(metadata, "audio", "Export")
    if len(metadata["streams"]) != 2:
        raise RenderError(f"Expected exactly one video and one audio stream: {path}")
    if (video.get("width"), video.get("height"), video.get("codec_name"), video.get("pix_fmt")) != (width, height, "h264", "yuv420p"):
        raise RenderError(f"Expected {width}x{height} H.264 yuv420p video: {path}")
    if video.get("sample_aspect_ratio") not in (None, "1:1"):
        raise RenderError(f"Expected square pixels: {path}")
    if audio.get("codec_name") != "aac" or audio.get("channels") != 2:
        raise RenderError(f"Expected stereo AAC audio: {path}")
    if "mp4" not in metadata.get("format", {}).get("format_name", "").split(","):
        raise RenderError(f"Expected an MP4 container: {path}")
    lengths = [stream_duration(video), stream_duration(audio)]
    try:
        lengths.append(float(metadata["format"]["duration"]))
    except (KeyError, ValueError, TypeError):
        lengths.append(None)
    if any(value is None or not math.isfinite(value) or abs(value - duration) > 0.1 for value in lengths):
        raise RenderError(f"Expected every stream and container to last {duration:g}s: {path}; got {lengths}")
    verified = {"full_decode": True, "resolution": [width, height], "duration": duration,
                "video_codec": "h264", "audio_codec": "aac", "channels": 2,
                "verified_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    # create_original.py retains its full-decode result with the exact source
    # SHA. A matching locally generated proof lets us reuse that work; stream
    # geometry, codecs and lengths are still independently checked above.
    prior_duration = prior_verification.get("duration") if prior_verification else None
    prior_decoded = prior_verification.get("decoded_seconds", prior_duration) if prior_verification else None
    if (prior_verification and prior_verification.get("full_decode_passed") is True
            and prior_verification.get("sha256") == digest(path)
            and type(prior_duration) in (int, float) and math.isfinite(prior_duration)
            and abs(prior_duration - duration) <= 0.1
            and type(prior_decoded) in (int, float) and math.isfinite(prior_decoded)
            and abs(prior_decoded - duration) <= 0.15):
        verified.update({"decoded_seconds": prior_decoded, "reused_source_full_decode": True})
        return verified
    decoded = run_ffmpeg(
        ["-err_detect", "explode", "-threads", "2", "-i", str(path),
         "-map", "0:v:0", "-map", "0:a:0", "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
        timeout=max(120, duration * 2),
    )
    timings = [int(line.split("=", 1)[1]) / 1_000_000 for line in decoded.stdout.splitlines() if line.startswith("out_time_us=") and line.split("=", 1)[1].lstrip("-").isdigit()]
    if not timings or abs(max(timings) - duration) > 0.15:
        raise RenderError(f"Full decode did not reach the expected end of {path}.")
    verified["decoded_seconds"] = max(timings)
    return verified


def temporary_output(destination: Path, writer: Any, verifier: Any) -> dict[str, Any]:
    if os.path.lexists(destination):
        raise RenderError(f"Refusing to overwrite an unverified existing file: {destination}")
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=destination.suffix, dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        writer(temporary)
        verification = verifier(temporary)
        os.link(temporary, destination)
        return verification
    finally:
        temporary.unlink(missing_ok=True)


def make_short(source: Path, destination: Path, short: dict[str, Any]) -> dict[str, Any]:
    if not FONT.is_file():
        raise RenderError("Install fonts-dejavu-core for the Shorts invitation text.")
    framing = f"(iw-ow)*{horizontal_crop(short):.12g}"
    cta_second_line = CTA_SECOND_LINES[short.get("cta", "channel")]
    invite_start = max(0, short["duration"] - 6)
    filters = (
        "setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase:"
        "force_divisible_by=2:out_range=tv,"
        f"crop=1080:1920:{framing}:(ih-oh)/2,setsar=1,fps=30,"
        f"drawtext=fontfile={FONT}:text='Watch the full hour':fontcolor=white:fontsize=58:"
        f"x=(w-tw)/2:y=1350:box=1:boxcolor=black@0.65:boxborderw=18:enable='gte(t,{invite_start:g})',"
        f"drawtext=fontfile={FONT}:text='{cta_second_line}':fontcolor=white:fontsize=50:"
        f"x=(w-tw)/2:y=1440:box=1:boxcolor=black@0.65:boxborderw=18:enable='gte(t,{invite_start:g})',"
        "format=yuv420p"
    )
    def writer(path: Path) -> None:
        run_ffmpeg([
            "-ss", str(short["start"]), "-threads", "2", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0", "-vf", filters, "-af", "asetpts=PTS-STARTPTS",
            "-t", str(short["duration"]), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-threads", "2", "-pix_fmt", "yuv420p", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "-y", str(path),
        ], timeout=max(300, short["duration"] * 10))
    return temporary_output(destination, writer, lambda path: verify_media(path, 1080, 1920, short["duration"]))


def make_frame(source: Path, destination: Path, offset: float, width: int, height: int) -> dict[str, Any]:
    def writer(path: Path) -> None:
        run_ffmpeg(["-ss", str(offset), "-threads", "2", "-i", str(source),
                    "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={width}:{height}",
                    "-q:v", "3", "-threads", "1", "-y", str(path)])
    def verifier(path: Path) -> dict[str, Any]:
        video = stream_of_type(probe(path), "video", "Thumbnail")
        if (video.get("width"), video.get("height")) != (width, height) or path.stat().st_size >= 2 * 1024 * 1024:
            raise RenderError("Thumbnail must have its expected dimensions and be under 2 MiB.")
        return {"resolution": [width, height], "source_offset": offset}
    return temporary_output(destination, writer, verifier)


def finished_thumbnail_recipe(source: Path) -> list[Any]:
    return ["finished-thumbnail", digest(source), 1280, 720, "contain-lanczos-jpeg-q3-v1"]


def make_finished_thumbnail(source: Path, destination: Path) -> dict[str, Any]:
    """Normalize finished artwork; preserve its complete design without cropping."""
    source_hash = digest(source)
    def writer(path: Path) -> None:
        run_ffmpeg([
            "-threads", "1", "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v", "3", "-threads", "1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1", "-y", str(path),
        ])
    def verifier(path: Path) -> dict[str, Any]:
        image = stream_of_type(probe(path), "video", "Finished thumbnail")
        if (image.get("width"), image.get("height"), image.get("codec_name")) != (1280, 720, "mjpeg"):
            raise RenderError("The supplied thumbnail did not produce a 1280x720 JPEG.")
        if path.stat().st_size >= 2 * 1024 * 1024:
            raise RenderError("The normalized thumbnail exceeds YouTube's 2 MiB limit.")
        if digest(source) != source_hash:
            raise RenderError("The finished thumbnail changed during normalization.")
        return {"resolution": [1280, 720], "source_sha256": source_hash,
                "normalization": "contain-lanczos-jpeg-q3-v1", "bytes": path.stat().st_size}
    return temporary_output(destination, writer, verifier)


def short_recipe(short: dict[str, Any]) -> list[Any]:
    # Keep existing default recipes stable so previously verified exports remain
    # reusable. New controls are explicit additions to the content recipe.
    recipe = [short["start"], short["duration"], short["framing"], INVITATION]
    if "crop_position" in short:
        recipe.append({"crop_position": horizontal_crop(short)})
    if short.get("cta", "channel") != "channel":
        recipe.append({"cta": short["cta"]})
    return recipe


def cached_artifact(manifest: dict[str, Any], key: str, path: Path, recipe: str) -> dict[str, Any] | None:
    record = manifest.setdefault("artifacts", {}).get(key)
    if record is not None:
        if not isinstance(record, dict):
            raise RenderError(f"Invalid cached artifact record for {key}.")
        if record.get("recipe_sha256") != recipe:
            raise RenderError(f"The media recipe for {key} changed. Use a new output directory to preserve verified renders.")
        if not path.is_file() or path.is_symlink() or digest(path) != record.get("sha256"):
            raise RenderError(f"Cached {key} is missing or changed; refusing to reuse it.")
        return record
    if os.path.lexists(path):
        raise RenderError(f"Untracked output exists: {path}. Inspect it and choose a new output directory.")
    return None


def prepare(episode_path: Path, output_dir: Path, long_override: Path | None = None) -> Path:
    require_tools()
    episode_path = episode_path.expanduser().resolve()
    config = validate_episode(json.loads(episode_path.read_text(encoding="utf-8")))
    raw_source = long_override or Path(text_field(config, "long_video"))
    source = raw_source.expanduser()
    if not source.is_absolute():
        source = (Path.cwd() if long_override else episode_path.parent) / source
    source = source.resolve()
    if not source.is_file():
        raise RenderError(f"Provide the existing verified one-hour MP4 at {source}.")
    supplied_thumbnail = None
    thumbnail_recipe = None
    if config.get("thumbnail_path"):
        supplied_thumbnail = Path(config["thumbnail_path"]).expanduser()
        if not supplied_thumbnail.is_absolute():
            supplied_thumbnail = episode_path.parent / supplied_thumbnail
        supplied_thumbnail = supplied_thumbnail.resolve()
        if not supplied_thumbnail.is_file():
            raise RenderError(f"Finished thumbnail does not exist: {supplied_thumbnail}")
        stream_of_type(probe(supplied_thumbnail), "video", "Finished thumbnail")
        thumbnail_recipe = finished_thumbnail_recipe(supplied_thumbnail)
    source_digest = digest(source)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "bundle.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("bundle_id") != config["bundle_id"]:
            raise RenderError("Output directory belongs to a different bundle or schema.")
        if manifest.get("source_sha256") != source_digest:
            raise RenderError("The source long video changed. Keep this bundle and use a new output directory.")
    else:
        manifest = {"schema_version": SCHEMA_VERSION, "bundle_id": config["bundle_id"],
                    "source_sha256": source_digest, "artifacts": {}, "videos": []}
    manifest.update({"intended_channel_id": config["intended_channel_id"],
                     "intended_channel_handle": config["intended_channel_handle"], "theme": config["theme"],
                     "playlist_id": config.get("playlist_id"), "recording_credits": config["recording_credits"],
                     "requested_privacy": "unlisted", "schedule_enabled": False, "ready": False})

    def artifact(key: str, destination: Path, recipe_data: Any, creator: Any) -> dict[str, Any]:
        recipe_hash = json_digest([RECIPE_VERSION, source_digest, recipe_data])
        record = cached_artifact(manifest, key, destination, recipe_hash)
        if record:
            print(f"Reusing verified {key}: {destination.name}", file=sys.stderr)
            return record
        verification = creator(destination)
        record = {"path": str(destination), "sha256": digest(destination),
                  "recipe_sha256": recipe_hash, "verification": verification}
        manifest["artifacts"][key] = record
        atomic_json(manifest_path, manifest)
        return record

    long_path = output_dir / "long.mp4"
    def reuse_long(destination: Path) -> dict[str, Any]:
        print("Verifying the full existing one-hour video before reuse…", file=sys.stderr)
        provenance_path = source.with_suffix(".provenance.json")
        prior = None
        if provenance_path.is_file():
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            if isinstance(provenance, dict) and provenance.get("schema_version") == 1 and isinstance(provenance.get("verification"), dict):
                prior = provenance["verification"]
        verification = verify_media(source, 1920, 1080, 3600, prior)
        if verification.get("reused_source_full_decode"):
            print("Reused SHA-matched original-source full-decode verification.", file=sys.stderr)
        if digest(source) != source_digest:
            raise RenderError("The source video changed during verification.")
        # Copying retained bytes to a disposable output file does not reencode.
        # It also prevents later input edits from changing a verified bundle.
        def verify_copy(path: Path) -> dict[str, Any]:
            if digest(path) != source_digest:
                raise RenderError("Source video changed while copying; the original verified bytes were not retained.")
            return verification
        temporary_output(destination, lambda path: shutil.copyfile(source, path), verify_copy)
        return verification
    long_record = artifact("long", long_path, ["long", 3600], reuse_long)
    if supplied_thumbnail:
        thumbnail = artifact("thumbnail", output_dir / "thumbnail.jpg", thumbnail_recipe,
                             lambda path: make_finished_thumbnail(supplied_thumbnail, path))
        if digest(supplied_thumbnail) != thumbnail_recipe[1]:
            raise RenderError("The supplied thumbnail changed after its cache key was computed.")
    else:
        thumbnail = artifact("thumbnail", output_dir / "thumbnail.jpg", ["thumbnail", 5, 1280, 720],
                             lambda path: make_frame(long_path, path, 5, 1280, 720))
    videos = [{"key": "long", "path": str(long_path), "sha256": long_record["sha256"],
               "title": config["title"], "description": description(config["description"], config["recording_credits"]),
               "thumbnail": thumbnail["path"], "thumbnail_sha256": thumbnail["sha256"],
               "verification": long_record["verification"]}]
    for index, short in enumerate(config["shorts"], 1):
        key = f"short{index}"
        path = output_dir / f"{key}.mp4"
        recipe = short_recipe(short)
        record = artifact(key, path, recipe, lambda destination, item=short: make_short(long_path, destination, item))
        cover = artifact(f"{key}-cover", output_dir / f"{key}-cover.jpg", [recipe, short["cover_offset"]],
                         lambda destination, item=short, video=path: make_frame(video, destination, item["cover_offset"], 1080, 1920))
        videos.append({"key": key, "path": str(path), "sha256": record["sha256"], "title": short["title"],
                       "description": description(short["description"], short.get("recording_credits", config["recording_credits"]), True),
                       "recording_credits": short.get("recording_credits", config["recording_credits"]),
                       "cover_frame": cover["path"], "cover_frame_sha256": cover["sha256"],
                       "suggested_cover_offset": short["cover_offset"], "source_start": short["start"],
                       "framing": short["framing"], "crop_position": horizontal_crop(short),
                       "cta": short.get("cta", "channel"), "verification": record["verification"]})
    for video in videos:
        video.update({"made_for_kids": config["made_for_kids"], "contains_synthetic_media": config["contains_synthetic_media"]})
    manifest["videos"] = videos
    manifest["ready"] = True
    manifest["studio_manual_steps"] = [
        "Review all three videos; publish the full hour before either Short.",
        "Set each Short's Related video to the full upload in YouTube Studio (not exposed by YouTube Data API v3).",
        "Select each suggested Short cover frame in the YouTube app/Studio where that control is available.",
        "Confirm any channel-specific disclosures and playlist restrictions in Studio.",
    ]
    atomic_json(manifest_path, manifest)
    lines = [f"# Upload review: {config['theme']}", "", f"Bundle: {config['bundle_id']}",
             f"Intended channel: {config['intended_channel_id'] or config['intended_channel_handle']}", "Requested visibility: unlisted for all three.",
             "Schedule: disabled. Publication remains manual.", ""]
    for video in videos:
        lines += [f"## {video['key']}: {video['title']}", "", video["description"], "",
                  f"File: {Path(video['path']).name}", f"SHA256: {video['sha256']}",
                  f"Made for kids: {video['made_for_kids']}", f"Contains synthetic media: {video['contains_synthetic_media']}", ""]
        if video.get("cover_frame"):
            lines += [f"Suggested cover: {Path(video['cover_frame']).name} at {video['suggested_cover_offset']:g}s.", ""]
    lines += ["## Manual Studio staging", "",
              f"1. Sign in to https://studio.youtube.com/ and select {config['intended_channel_id'] or config['intended_channel_handle']}. Check the channel before uploading.",
              "2. Check Studio's Content list for an existing upload of each file/title before creating another. Preserve existing uploads. Record every successful video ID immediately.",
              "3. Upload long.mp4. Copy its title and description above, upload thumbnail.jpg, choose the intended playlist (not supplied in this bundle), and review the audience and altered/synthetic-content settings shown above.",
              "4. Explicitly choose Unlisted and save. Do not select Public or Schedule. If YouTube restricts visibility, retain Private and record the restriction plus the Studio edit link.",
              "5. Upload short1.mp4 and short2.mp4 with their own titles and descriptions above. Keep both Unlisted. Add the full video's watch URL to each description after its upload ID is known.",
              "6. For each Short, set Related video to the full upload in Studio. This may require advanced-feature access. Use the suggested cover image/timestamp in the YouTube app or Studio if that control is available; it is a suggested frame, not an automatically applied thumbnail.",
              "7. Review and play all three videos in Studio. Publish the full video first, then the Shorts. The daily schedule remains disabled until the full test is approved and an Asia/Nicosia start time is confirmed.",
              "", "Studio link format after an upload succeeds: https://studio.youtube.com/video/VIDEO_ID/edit", "",
              "## Remaining channel-specific choices", "",
              "- Select the intended playlist; no playlist ID was provided.",
              "- Confirm audience, altered/synthetic-content disclosure, and any other applicable channel settings before publication.",
              "- Select each Short cover frame and Related video manually where supported.", "",
              "## Manual Studio checks", "", *(f"- {item}" for item in manifest["studio_manual_steps"]), ""]
    details = output_dir / "upload-details.md"
    details.write_text("\n".join(lines), encoding="utf-8")
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=Path, required=True, help="approved episode JSON with titles, credits, disclosures and Short segments")
    parser.add_argument("--output-dir", type=Path, required=True, help="new or matching cached review bundle directory")
    parser.add_argument("--long-video", type=Path, help="override the episode's long_video path, e.g. a cloud-downloaded file")
    args = parser.parse_args(argv)
    try:
        result = prepare(args.episode, args.output_dir, args.long_video)
    except KeyboardInterrupt:
        print("Bundle preparation cancelled. Completed verified artifacts are retained for reuse.", file=sys.stderr)
        return 130
    except (RenderError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Verified review bundle: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
