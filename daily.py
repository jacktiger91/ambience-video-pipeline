#!/usr/bin/env python3
"""Prepare an approved episode in a cloud job without logging secret source URLs."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
MAX_SOURCE_BYTES = 8 * 1024**3
ASSET_NAMES = ("audio", "backdrop", "thumbnail")


class JobError(Exception):
    pass


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise JobError("Source download tried to redirect to a non-HTTPS destination.")
        return super().redirect_request(request, file, code, message, headers, url)


def download(secret_name: str, destination: Path) -> None:
    """Download through inherited proxy/CA settings; keep URL/query out of logs."""
    value = os.environ.get(secret_name, "")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise JobError(f"{secret_name} must contain a downloadable HTTPS URL without embedded user credentials.")
    digest_name = secret_name.removesuffix("_URL") + "_SHA256"
    expected = os.environ.get(digest_name, "").lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise JobError(f"{digest_name} must be a SHA-256 hex digest when provided.")
    digest = hashlib.sha256()
    try:
        opener = urllib.request.build_opener(HTTPSRedirectHandler())
        with opener.open(value, timeout=120) as response, destination.open("xb") as output:
            # A redirect must not downgrade TLS. No response headers are logged.
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise JobError(f"{secret_name} redirected to a non-HTTPS destination.")
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_SOURCE_BYTES:
                    raise JobError(f"{secret_name} exceeds the 8 GiB source limit.")
                digest.update(chunk)
                output.write(chunk)
        if not count:
            raise JobError(f"{secret_name} downloaded an empty file.")
        if expected and digest.hexdigest() != expected:
            raise JobError(f"{secret_name} failed its configured SHA-256 integrity check.")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        # urllib exceptions can include signed URLs and HTTP response details.
        raise JobError(f"Could not download {secret_name}; check its access and expiry in the secret store.") from None


def run(*arguments: str) -> None:
    result = subprocess.run([sys.executable, *arguments])
    if result.returncode:
        raise JobError(f"{Path(arguments[0]).name} failed (exit {result.returncode}); review the preceding diagnostics.")


def prepare(args: argparse.Namespace) -> None:
    episode = args.episode.expanduser().resolve()
    try:
        data = json.loads(episode.read_text())
    except (OSError, json.JSONDecodeError):
        raise JobError("Episode configuration is missing or invalid JSON.") from None
    if not isinstance(data, dict):
        raise JobError("Episode configuration must be a JSON object.")
    bundle_id = data.get("bundle_id", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", bundle_id):
        raise JobError("bundle_id must contain 1–80 letters, numbers, underscores, or hyphens.")
    expected_channel = os.environ.get("YOUTUBE_CHANNEL_ID")
    if expected_channel and data.get("intended_channel_id") and data["intended_channel_id"] != expected_channel:
        raise JobError("Episode channel ID does not match the protected environment's YOUTUBE_CHANNEL_ID.")
    if "REPLACE" in json.dumps(data):
        raise JobError("Replace all example metadata with your approved recording details before running.")
    verified_assets = None
    moving_visual = None
    if args.source_mode in {"verified-assets", "new-original"} or data.get("brief_kind") == "daily_production":
        if args.source_mode == "generate-original":
            raise JobError("Daily production requires real field recordings; the legacy synthetic generator cannot fill missing assets.")
        if args.source_mode == "reuse-long":
            raise JobError("For a dated production brief, retry verified-assets so its source hashes and history can be checked before reusing the long render.")
        verified_assets = validate_assets(episode, data, args.asset_history)
        thumbnail_path = data.get("thumbnail_path")
        if not thumbnail_path or resolve_asset_path(episode, thumbnail_path) != verified_assets["thumbnail"]["path"]:
            raise JobError("thumbnail_path must reference this episode's newly verified thumbnail.")
        moving_visual = validate_moving_visual(episode, data, verified_assets["backdrop"].get("scene_id"))
    elif args.source_mode == "generate-original" and not args.legacy_synthetic:
        raise JobError("The synthetic generator is retained for comparison only. Use --legacy-synthetic explicitly; it is not the production daily workflow.")
    # Check metadata, Short timings, and disclosures before an expensive hour render.
    from prepare_bundle import validate_episode
    from render import RenderError
    try:
        validate_episode(data)
    except RenderError as exc:
        raise JobError(str(exc)) from None
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ambience-source-") as temp_name:
        temporary = Path(temp_name)
        if args.source_mode == "reuse-long":
            if args.long_video:
                long_video = args.long_video.expanduser().resolve()
            elif os.environ.get("SOURCE_LONG_VIDEO_URL"):
                long_video = temporary / "verified-long.mp4"
                download("SOURCE_LONG_VIDEO_URL", long_video)
            else:
                long_video = (episode.parent / str(data.get("long_video", ""))).resolve()
                if not long_video.is_file():
                    raise JobError("Provide --long-video, an existing config long_video, or SOURCE_LONG_VIDEO_URL.")
        elif args.source_mode == "generate-original":
            theme = data.get("generator_theme")
            seed = data.get("generator_seed")
            if not isinstance(theme, str) or not isinstance(seed, int):
                raise JobError("A generated episode needs generator_theme and integer generator_seed in its config.")
            long_video = output.parent / f"source-{bundle_id}.mp4"
            if long_video.exists():
                raise JobError("The generated long MP4 already exists. Reuse it with --source-mode reuse-long --long-video instead of regenerating it.")
            run(str(ROOT / "create_original.py"), "--theme", theme, "--seed", str(seed),
                "--duration", "3600", "--output", str(long_video), "--assets-dir", str(temporary / "assets"))
        else:
            if verified_assets is None:
                raise JobError("New daily renders require verified real audio, a new backdrop, and a new thumbnail.")
            visual = moving_visual["path"]
            audio = verified_assets["audio"]["path"]
            long_video = output.parent / f"source-{bundle_id}.mp4"
            receipt_path = long_video.with_suffix(".source-receipt.json")
            source_record = {name: record["sha256"] for name, record in verified_assets.items()}
            source_record["moving_visual"] = moving_visual["sha256"]
            source_record["loop_audio"] = args.loop_audio
            if long_video.exists():
                try:
                    receipt = json.loads(receipt_path.read_text())
                except (OSError, ValueError):
                    raise JobError("Existing long render has no source receipt. Preserve it and review its provenance before reusing it.") from None
                if receipt.get("sources") != source_record or receipt.get("long_sha256") != file_digest(long_video):
                    raise JobError("Existing long render or its source assets changed; refusing to overwrite or reuse it.")
            else:
                command = [
                    str(ROOT / "render.py"), "--visual", str(visual), "--audio", str(audio),
                    "--duration", "01:00:00", "--output", str(long_video),
                ]
                if args.loop_audio:
                    command.append("--loop-audio")
                run(*command)
                receipt_path.write_text(json.dumps({"sources": source_record, "long_sha256": file_digest(long_video)}, indent=2) + "\n")
        run(str(ROOT / "prepare_bundle.py"), "--episode", str(episode),
            "--long-video", str(long_video), "--output-dir", str(output))
    if verified_assets is not None:
        # Recheck after encoding, so edited input files cannot silently become
        # the provenance for a render made from different bytes.
        validate_assets(episode, data, args.asset_history)
        if validate_moving_visual(episode, data, verified_assets["backdrop"].get("scene_id"))["sha256"] != moving_visual["sha256"]:
            raise JobError("The moving visual changed during encoding; preserve and review the result.")
        record_asset_use(data, verified_assets, args.asset_history)
    print(f"Prepared bundle: {output / 'bundle.json'}")


def check_artifact_budget(args: argparse.Namespace) -> None:
    # This is an additional bound, not a billing API. The protected environment
    # must separately have paid Actions/storage spending blocked at the owner.
    if os.environ.get("ZERO_SPEND_CONFIRMED") != "true":
        raise JobError("Set ZERO_SPEND_CONFIRMED=true only after blocking paid Actions/storage spending in GitHub billing.")
    if not args.directory.is_dir() or not (args.directory / "bundle.json").is_file():
        raise JobError("The verified review bundle is missing; there is no artifact to share.")
    size = sum(p.stat().st_size for p in args.directory.rglob("*") if p.is_file())
    if size > args.max_mib * 1024**2:
        raise JobError(f"Review bundle exceeds the {args.max_mib} MiB artifact limit. Use your existing free cloud storage for the long MP4.")
    print(f"Review artifact size: {size / 1024**2:.1f} MiB (limit {args.max_mib} MiB).")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_asset_path(episode: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise JobError("Asset and evidence paths must be explicitly supplied.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else episode.parent / path).resolve()


def validate_moving_visual(episode: Path, data: dict, scene_id: str | None) -> dict:
    """Require a reviewed moving scene; a still backdrop is only an artwork asset."""
    record = data.get("moving_visual")
    if not isinstance(record, dict) or record.get("status") != "verified":
        raise JobError("Prepare and review a moving visual first. Daily MP4s must show scene motion; a still backdrop cannot be substituted.")
    path = resolve_asset_path(episode, record.get("path"))
    sha = record.get("sha256", "")
    if (not path.is_file() or path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}
            or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha) or file_digest(path) != sha):
        raise JobError("The moving visual must be a video matching its reviewed SHA-256; still images are not production visuals.")
    try:
        report = json.loads(resolve_asset_path(episode, record.get("qa_report_path")).read_text())
    except (OSError, ValueError):
        raise JobError("A readable motion review report is required.") from None
    if (not isinstance(report, dict) or report.get("visual_sha256") != sha
            or not scene_id or record.get("scene_id") != scene_id or report.get("scene_id") != scene_id
            or any(report.get(flag) is not True for flag in ("subject_motion_visible", "camera_fixed", "matches_concept"))
            or not isinstance(report.get("review_notes"), str) or not report["review_notes"].strip()):
        raise JobError("Review the actual moving scene: visible subject motion, fixed camera, matching concept and hash-bound observations are required.")
    from render import probe, stream_of_type, stream_duration, positive_number, IMAGE_DEMUXERS, RenderError
    try:
        metadata = probe(path)
        stream = stream_of_type(metadata, "video", "Moving visual")
        duration = stream_duration(stream) or positive_number(metadata.get("format", {}).get("duration"))
        formats = set(metadata.get("format", {}).get("format_name", "").split(","))
        if not duration or duration < 1 or formats & IMAGE_DEMUXERS or stream.get("nb_frames") in (1, "1"):
            raise JobError("The moving visual must be a video clip of at least one second, not a still image.")
    except RenderError as exc:
        raise JobError(str(exc)) from None
    if duration < 3600 and report.get("loop_seam_reviewed") is not True:
        raise JobError("Review the visual loop transition before repeating this clip for the full hour.")
    return {"path": path, "sha256": sha}


def read_history(path: Path | None) -> dict:
    if path is None or not path.is_file():
        raise JobError("A retained asset-history file is required. Record existing channel assets first; do not start a blank history on every cloud run.")
    try:
        history = json.loads(path.read_text())
    except (OSError, ValueError):
        raise JobError("The asset history cannot be read; preserve it and resolve the problem before rendering.") from None
    if not isinstance(history, dict) or history.get("schema_version") != 1 or not isinstance(history.get("episodes"), dict):
        raise JobError("Unsupported asset-history format.")
    if any(not isinstance(value, dict) or not isinstance(value.get("assets"), dict) for value in history["episodes"].values()):
        raise JobError("Malformed asset-history entry; preserve it and review the prior records.")
    return history


def validate_assets(episode: Path, data: dict, history_path: Path | None) -> dict:
    """Verify documented approval and actual files, without inventing permissions."""
    history = read_history(history_path)
    if not isinstance(data, dict):
        raise JobError("The episode must be a JSON object.")
    assets = data.get("assets", {})
    if not isinstance(assets, dict):
        raise JobError("The episode assets must be an object.")
    verified = {}
    for name in ASSET_NAMES:
        record = assets.get(name)
        if not isinstance(record, dict) or record.get("status") != "verified":
            raise JobError(f"{name} is unverified. A future concept is not a licensed, QA-checked asset.")
        if name == "audio" and record.get("kind") != "real_field_recording":
            raise JobError("Production audio must use real field recordings. Synthetic noise is not a fallback.")
        if name == "backdrop" and record.get("kind") not in {"photographic_ai_scene", "photograph", "video"}:
            raise JobError("Production backdrops must be new photographic scenes, with their AI or photographic origin stated truthfully.")
        if name == "thumbnail" and record.get("kind") != "branded_thumbnail":
            raise JobError("Supply a new branded thumbnail, not an old scene with a changed title.")
        if record.get("rights_verified") is not True or record.get("qa_passed") is not True:
            raise JobError(f"{name} requires documented rights verification and passed quality review.")
        if not isinstance(record.get("credits"), str) or not record["credits"].strip():
            raise JobError(f"Supply the actual {name} credits.")
        for evidence in ("rights_evidence_path", "qa_report_path"):
            path = resolve_asset_path(episode, record.get(evidence))
            if not path.is_file() or path.stat().st_size == 0:
                raise JobError(f"Missing {name} {evidence}; approval cannot be inferred from a filename or title.")
        path = resolve_asset_path(episode, record.get("path"))
        expected = record.get("sha256", "")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or not path.is_file() or file_digest(path) != expected:
            raise JobError(f"{name} file is missing or does not match its verified SHA-256.")
        verified[name] = {"path": path, "sha256": expected}
    scene_id = assets["backdrop"].get("scene_id")
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise JobError("The new backdrop needs its own scene_id.")
    if assets["thumbnail"].get("source_scene_id") != scene_id:
        raise JobError("The thumbnail must depict this episode's new scene, using the shared brand style.")
    verified["backdrop"]["scene_id"] = scene_id
    for previous_id, previous in history["episodes"].items():
        previous_assets = previous.get("assets", {})
        for name in ASSET_NAMES:
            old = previous_assets.get(name, {})
            if not isinstance(old, dict) or not isinstance(old.get("sha256_aliases", []), list):
                raise JobError("Malformed prior asset record; do not bypass history validation.")
            if previous_id == data.get("bundle_id"):
                if old and old.get("sha256") != verified[name]["sha256"]:
                    raise JobError("This bundle ID already identifies different assets. Keep the previous result and use a new reviewed revision.")
                continue
            if verified[name]["sha256"] in [old.get("sha256"), *old.get("sha256_aliases", [])]:
                raise JobError(f"The {name} is already used by {previous_id}; a new title does not make a new daily concept.")
            if name == "backdrop" and old.get("scene_id") == scene_id:
                raise JobError(f"Backdrop scene {scene_id} is already used by {previous_id}; create a genuinely new scene.")
    return verified


def record_asset_use(data: dict, assets: dict, path: Path) -> None:
    history = read_history(path)
    history["episodes"][data["bundle_id"]] = {
        "episode_date": data.get("episode_date"), "concept_id": data.get("concept_id"),
        "assets": {name: {key: value for key, value in record.items() if key != "path"}
                   for name, record in assets.items()},
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".asset-history-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(history, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_assets(args: argparse.Namespace) -> None:
    episode = args.episode.expanduser().resolve()
    try:
        data = json.loads(episode.read_text())
    except (OSError, ValueError):
        raise JobError("Episode is missing or invalid JSON.") from None
    validate_assets(episode, data, args.asset_history)
    print("Verified asset hashes, recorded rights/QA evidence, and distinct daily sources. No render or upload started.")


def load_catalog(path: Path) -> dict:
    try:
        catalog = json.loads(path.read_text())
    except (OSError, ValueError):
        raise JobError("A readable daily-concepts catalog is required.") from None
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise JobError("Unsupported daily-concepts catalog.")
    concepts = catalog.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        raise JobError("The catalog must contain documented concepts.")
    publishing = None
    if catalog.get("publishing_copy"):
        try:
            publishing = json.loads((path.parent / catalog["publishing_copy"]).read_text())
        except (OSError, ValueError, TypeError):
            raise JobError("The daily title/description copy cannot be read.") from None
        if (not isinstance(publishing, dict) or publishing.get("schema_version") != 1
                or not isinstance(publishing.get("concepts"), dict)):
            raise JobError("Unsupported daily title/description copy format.")
    seen_ids, seen_backdrops = set(), set()
    for concept in concepts:
        if not isinstance(concept, dict):
            raise JobError("Each catalog concept must be an object.")
        for field in ("id", "title", "theme", "sound_family", "setting", "framing", "backdrop_brief", "thumbnail_brief"):
            if not isinstance(concept.get(field), str) or not concept[field].strip():
                raise JobError(f"Every concept needs a documented {field}.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", concept["id"]):
            raise JobError("Concept IDs must use lowercase letters, digits and hyphens.")
        backdrop = concept["backdrop_brief"].strip().casefold()
        if concept["id"] in seen_ids or backdrop in seen_backdrops:
            raise JobError("Concept IDs and backdrop briefs must be distinct; renamed copies are not daily ideas.")
        seen_ids.add(concept["id"])
        seen_backdrops.add(backdrop)
        requirements = concept.get("audio_requirements", {})
        if requirements.get("kind") != "real_field_recording":
            raise JobError("Every production concept must require real field recordings.")
        if not requirements.get("required_sounds") or not requirements.get("license_requirement"):
            raise JobError("Every concept needs specific audio and licensing requirements.")
        if publishing is not None:
            copy = publishing["concepts"].get(concept["id"])
            if not isinstance(copy, dict) or copy.get("status") not in {"draft", "ready"}:
                raise JobError(f"Missing publishing copy for {concept['id']}.")
            shorts = copy.get("shorts")
            if not isinstance(shorts, list) or len(shorts) != 2:
                raise JobError(f"{concept['id']} needs separate copy for two Shorts.")
            for item in [copy.get("long"), *shorts]:
                if not isinstance(item, dict):
                    raise JobError(f"Invalid publishing copy for {concept['id']}.")
                for field, limit in (("title", 100), ("description", 5000)):
                    value = item.get(field)
                    if not isinstance(value, str) or not value.strip() or len(value) > limit:
                        raise JobError(f"{concept['id']} needs a nonempty {field} within YouTube's {limit}-character limit.")
            concept["publishing_copy"] = copy
    return catalog


def plan(args: argparse.Namespace) -> None:
    """Write a truthful daily brief; a brief does not create or license any media."""
    catalog = load_catalog(args.catalog)
    try:
        day = date.fromisoformat(args.date) if args.date else datetime.now(ZoneInfo("Asia/Nicosia")).date()
        first_day = date.fromisoformat(catalog["rotation_start_date"])
    except (ValueError, KeyError):
        raise JobError("Provide YYYY-MM-DD dates and a valid catalog rotation_start_date.") from None
    offset = (day - first_day).days
    if offset < 0 or offset >= len(catalog["concepts"]):
        raise JobError("No new concept is approved for that date. Extend the catalog; old themes are never silently recycled.")
    concept = catalog["concepts"][offset]
    fingerprint = hashlib.sha256(json.dumps({"day": day.isoformat(), "concept": concept, "brand": catalog.get("brand", {})}, sort_keys=True).encode()).hexdigest()
    data = {
        "schema_version": 2, "brief_kind": "daily_production",
        "bundle_id": f"{day.isoformat()}-{concept['id']}", "episode_date": day.isoformat(),
        "timezone": "Asia/Nicosia", "concept_id": concept["id"], "brief_fingerprint": fingerprint,
        "content_status": "awaiting_verified_assets", "schedule_enabled": False, "publishing": "manual_studio_only",
        "brand": catalog.get("brand", {}),
        "intended_channel_handle": catalog.get("brand", {}).get("channel_handle", "@QuietHoursRelaxingSounds"),
        **{key: concept[key] for key in ("title", "theme", "sound_family", "setting", "framing", "backdrop_brief", "thumbnail_brief", "audio_requirements")},
        "description": None, "recording_credits": None, "made_for_kids": None, "contains_synthetic_media": None,
        "thumbnail_path": None,
        "motion_requirements": concept.get("motion_requirements", {"required": True, "camera": "fixed", "applies_to": ["long", "short1", "short2"], "review_required": True}),
        "moving_visual": {"status": "unverified", "path": None, "sha256": None, "scene_id": None, "qa_report_path": None},
        "assets": {name: {"status": "unverified", "kind": "real_field_recording" if name == "audio" else "new_photographic_scene" if name == "backdrop" else "new_branded_thumbnail",
                          "path": None, "sha256": None, "credits": None, "rights_verified": False,
                          "rights_evidence_path": None, "qa_passed": False, "qa_report_path": None}
                   for name in ASSET_NAMES},
        "shorts": concept.get("shorts", []),
        "review_notes": [
            "Obtain and check real sound recordings; no synthetic-audio fallback.",
            "Create a new scene for this setting; do not retitle or recolor the Night Rain veranda.",
            "Create a new thumbnail from this scene using the shared brand design.",
            "Prepare visible subject motion for the full hour and both Shorts; do not use a held image or camera pan/zoom as the moving scene. Check motion remains visible in each final crop.",
            "Check source rights, audio quality, truthful credits, two Short moments and disclosure decisions before rendering.",
            "Image generation is available interactively; this queue does not imply unattended image generation or licensed assets already exist.",
        ],
    }
    data["assets"]["backdrop"]["scene_id"] = None
    data["assets"]["thumbnail"]["source_scene_id"] = None
    if "publishing_copy" in concept:
        copy = concept["publishing_copy"]
        data["concept_title"] = concept["title"]
        data["title"] = copy["long"]["title"]
        data["description"] = copy["long"]["description"]
        data["metadata_copy_status"] = copy["status"]
        data["shorts"] = [
            {"title": short["title"], "description": short["description"],
             "start": None, "duration": 25, "framing": "center", "crop_position": None,
             "cover_offset": 22, "cta": "quiet-hours"}
            for short in copy["shorts"]
        ]
        data["review_notes"].append("Titles and description bodies are prepared. Match them to the finished sound/scene, append actual per-video recording credits, and choose two distinct Short moments before export.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        try:
            existing = json.loads(args.output.read_text())
        except (OSError, ValueError):
            raise JobError("The existing brief cannot be read; it was not overwritten.") from None
        if existing.get("bundle_id") != data["bundle_id"] or existing.get("brief_fingerprint") != fingerprint:
            raise JobError("An existing brief has different content. Preserve it and choose a new reviewed output path.")
        print(f"Reusing existing daily brief: {args.output}; its asset and review updates were preserved.")
        return
    with args.output.open("x") as output:
        json.dump(data, output, indent=2)
        output.write("\n")
    print(f"Prepared daily brief: {args.output}. Assets are unverified; scheduling and publishing remain off.")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="prepare one approved episode; does not upload or publish")
    p.add_argument("--episode", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--source-mode", choices=("verified-assets", "reuse-long", "new-original", "generate-original"), default="verified-assets")
    p.add_argument("--asset-history", type=Path, help="retained nonsecret history of previous daily audio/backdrop/thumbnail hashes")
    p.add_argument("--legacy-synthetic", action="store_true", help="explicit comparison-only opt-in for the old synthetic generator")
    p.add_argument("--long-video", type=Path)
    p.add_argument("--loop-audio", action="store_true", help="explicitly allow repeating audio for a new original")
    p.set_defaults(function=prepare)
    p = commands.add_parser("check-artifact-budget", help="bound artifact size after a confirmed zero-spend account setting")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--max-mib", type=int, default=400)
    p.set_defaults(function=check_artifact_budget)
    p = commands.add_parser("check-assets", help="check real-audio rights/QA evidence and reject reused daily assets")
    p.add_argument("--episode", type=Path, required=True)
    p.add_argument("--asset-history", type=Path, required=True)
    p.set_defaults(function=check_assets)
    p = commands.add_parser("plan", help="draft the next distinct production concept; does not create media or schedule")
    p.add_argument("--catalog", type=Path, default=ROOT / "config" / "daily-concepts.json")
    p.add_argument("--date", help="YYYY-MM-DD; defaults to today's Asia/Nicosia date")
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(function=plan)
    args = parser.parse_args()
    try:
        args.function(args)
    except (JobError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
