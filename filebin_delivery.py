#!/usr/bin/env python3
"""Deliver a verified daily review bundle and its publishing copy to Filebin.

This is a temporary download handoff, not a scheduler or a YouTube publisher.
Only the named media artifacts and generated upload details are sent. Local
manifests, logs, credentials and arbitrary files in the bundle directory stay local.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.request


BASE = "https://filebin.net"
VIDEO_KEYS = ("long", "short1", "short2")
IMAGE_KEYS = ("thumbnail", "short1-cover", "short2-cover")


class DeliveryError(Exception):
    pass


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise DeliveryError("A required local JSON document is missing or invalid.") from None
    if not isinstance(data, dict):
        raise DeliveryError("Expected a JSON object.")
    return data


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def asset_path(bundle_path: Path, value) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError("Every selected artifact needs an explicit file path.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else bundle_path.parent / path).resolve()


def verified_file(bundle_path: Path, record: dict, suffixes: set[str]) -> dict:
    path = asset_path(bundle_path, record.get("path"))
    sha = record.get("sha256", "")
    if path.suffix.lower() not in suffixes or not path.is_file() or not path.stat().st_size:
        raise DeliveryError("A selected artifact is missing, empty, or has the wrong media extension.")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or digest(path) != sha:
        raise DeliveryError("A selected artifact no longer matches its verified SHA-256.")
    return {"path": str(path), "size": path.stat().st_size, "sha256": sha}


def check_video(key: str, proof: dict) -> None:
    resolution = [1920, 1080] if key == "long" else [1080, 1920]
    duration = proof.get("duration")
    decoded = proof.get("decoded_seconds")
    if (proof.get("full_decode") is not True or proof.get("resolution") != resolution
            or proof.get("video_codec") != "h264" or proof.get("audio_codec") != "aac"
            or proof.get("channels") != 2 or not number(duration) or not number(decoded)
            or decoded < duration - 0.1):
        raise DeliveryError(f"{key} lacks passed end-to-end H.264/stereo AAC verification.")
    if (key == "long" and abs(duration - 3600) > 0.1) or (key != "long" and not 15 <= duration <= 30):
        raise DeliveryError(f"{key} has the wrong verified duration.")


def build_manifest(bundle_path: Path, backdrop: Path | None = None) -> tuple[dict, dict, str]:
    bundle_path = bundle_path.expanduser().resolve()
    bundle = read_json(bundle_path)
    bundle_id = bundle.get("bundle_id", "")
    if (bundle.get("schema_version") != 1 or bundle.get("ready") is not True
            or not isinstance(bundle_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", bundle_id)):
        raise DeliveryError("A finished, ready review bundle with a stable bundle_id is required.")
    if bundle.get("requested_privacy") != "unlisted" or bundle.get("schedule_enabled") is not False:
        raise DeliveryError("This handoff requires Unlisted review metadata and scheduling disabled.")
    artifacts, videos = bundle.get("artifacts"), bundle.get("videos")
    if not isinstance(artifacts, dict) or not isinstance(videos, list) or len(videos) != 3:
        raise DeliveryError("The bundle needs three verified videos and their image artifacts.")
    by_key = {item.get("key"): item for item in videos if isinstance(item, dict)}
    if set(by_key) != set(VIDEO_KEYS):
        raise DeliveryError("The bundle must contain exactly long, short1 and short2.")
    files, names = {}, {}
    for key in (*VIDEO_KEYS, *IMAGE_KEYS):
        artifact = artifacts.get(key)
        if not isinstance(artifact, dict) or not isinstance(artifact.get("verification"), dict):
            raise DeliveryError(f"Missing verified artifact: {key}.")
        item = verified_file(bundle_path, artifact, {".mp4"} if key in VIDEO_KEYS else {".jpg", ".jpeg", ".png"})
        name = f"{bundle_id}-{key}{Path(item['path']).suffix.lower()}"
        files[name], names[key] = item, name
        if key in VIDEO_KEYS:
            video = by_key[key]
            if video.get("sha256") != artifact["sha256"] or asset_path(bundle_path, video.get("path")) != Path(item["path"]):
                raise DeliveryError(f"{key} metadata does not match its verified artifact.")
            for proof in (artifact["verification"], video.get("verification", {})):
                if not isinstance(proof, dict):
                    raise DeliveryError(f"Missing video verification: {key}.")
                check_video(key, proof)
            for field, maximum in (("title", 100), ("description", 5000)):
                value = video.get(field)
                length = len(value.encode("utf-8")) if isinstance(value, str) and field == "description" else len(value) if isinstance(value, str) else 0
                if not isinstance(value, str) or not value.strip() or length > maximum:
                    raise DeliveryError(f"{key} needs its finished {field} within YouTube limits.")
            if "Recording credits:" not in video["description"] or not video["description"].split("Recording credits:", 1)[1].strip():
                raise DeliveryError(f"{key} needs its actual recording credits in the final description.")
            if any(type(video.get(flag)) is not bool for flag in ("made_for_kids", "contains_synthetic_media")):
                raise DeliveryError(f"{key} needs reviewed audience and disclosure settings.")
        else:
            expected = [1280, 720] if key == "thumbnail" else [1080, 1920]
            if artifact["verification"].get("resolution") != expected:
                raise DeliveryError(f"{key} lacks the expected image verification.")
    long = by_key["long"]
    if (asset_path(bundle_path, long.get("thumbnail")) != Path(files[names["thumbnail"]]["path"])
            or long.get("thumbnail_sha256") != files[names["thumbnail"]]["sha256"]):
        raise DeliveryError("The long video's thumbnail does not match the selected artifact.")
    for key in ("short1", "short2"):
        video, cover = by_key[key], files[names[key + "-cover"]]
        offset = video.get("suggested_cover_offset")
        if (asset_path(bundle_path, video.get("cover_frame")) != Path(cover["path"])
                or video.get("cover_frame_sha256") != cover["sha256"] or not number(offset)
                or not 0 <= offset < video["verification"]["duration"]):
            raise DeliveryError(f"{key} needs a matching suggested cover frame and timestamp.")
    if backdrop is not None:
        path = backdrop.expanduser().resolve()
        if not path.is_file():
            raise DeliveryError("The explicitly selected backdrop is missing.")
        selected = verified_file(bundle_path, {"path": str(path), "sha256": digest(path)}, {".jpg", ".jpeg", ".png"})
        files[f"{bundle_id}-backdrop{path.suffix.lower()}"] = selected
    lines = [f"Upload details: {bundle_id}", "", "Requested visibility: Unlisted for all three videos.",
             "Upload and publication are manual. Filebin delivery does not upload to YouTube.", ""]
    for key in VIDEO_KEYS:
        video = by_key[key]
        lines += [f"{key.upper()} — {names[key]}", "", "Title:", video["title"], "", "Description:", video["description"], "",
                  f"Made for kids: {str(video['made_for_kids']).lower()}",
                  f"Altered/synthetic content: {str(video['contains_synthetic_media']).lower()}"]
        if key == "long":
            lines.append("Thumbnail: " + names["thumbnail"])
        else:
            lines += [f"Suggested cover: {names[key + '-cover']} at {video['suggested_cover_offset']:g} seconds.",
                      "Set Related video to the full upload in YouTube Studio."]
        lines.append("")
    playlist = bundle.get("playlist_id")
    lines += ["Playlist: " + (str(playlist) if playlist else "Choose the intended channel playlist in Studio."),
              "Review all three exports, then publish the full hour before the Shorts.", ""]
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    files[f"{bundle_id}-upload-details.txt"] = {"text": text, "size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    # Stable across runner paths and harmless receipt timestamps. The generated
    # text's SHA binds titles, descriptions, credits and all public settings.
    expected = {name: {"size": item["size"], "sha256": item["sha256"]} for name, item in files.items()}
    identity = hashlib.sha256(json.dumps({"bundle_id": bundle_id, "files": expected}, sort_keys=True).encode()).hexdigest()
    return bundle, files, identity


def save_state(path: Path, state: dict, sync_command: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)
    if sync_command is not None:
        try:
            # The CLI supplies a command prefix; the saved state is always the
            # final argument so workflow callers cannot omit its required path.
            subprocess.run([*sync_command, str(path)], check=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise DeliveryError("Durable delivery-state synchronization failed; no further upload will run.") from None


class FilebinClient:
    def __init__(self, timeout: int = 900):
        self.timeout = timeout

    def listing(self, bin_id: str) -> dict:
        request = urllib.request.Request(f"{BASE}/{bin_id}", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"files": []}
            raise DeliveryError(f"Filebin listing failed with HTTP {exc.code}.") from None
        except (OSError, ValueError):
            raise DeliveryError("Could not read Filebin metadata; preserve delivery state and retry later.") from None
        if not isinstance(data, dict) or not isinstance(data.get("bin"), dict):
            raise DeliveryError("Filebin returned malformed metadata.")
        if data.get("files") is None and data["bin"].get("files") == 0:
            data["files"] = []
        if not isinstance(data.get("files"), list):
            raise DeliveryError("Filebin returned a malformed file listing.")
        return data

    def curl(self, arguments: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["curl", "--silent", "--show-error", "--proto", "=https", "--proto-redir", "=https",
                                   "--connect-timeout", "15", "--max-time", str(self.timeout), *arguments],
                                  capture_output=True, timeout=self.timeout + 15)
        except (OSError, subprocess.TimeoutExpired):
            raise DeliveryError("Filebin request did not complete within its deadline; preserve state before retrying.") from None

    def upload(self, bin_id: str, name: str, record: dict) -> None:
        with tempfile.TemporaryDirectory(prefix="filebin-post-") as directory:
            root = Path(directory)
            source = Path(record["path"]) if "path" in record else root / "upload-details.txt"
            if "text" in record:
                source.write_text(record["text"])
            if source.stat().st_size != record["size"] or digest(source) != record["sha256"]:
                raise DeliveryError("Selected bytes changed before upload.")
            response = root / "response.json"
            content_type = "text/plain; charset=utf-8" if "text" in record else "video/mp4" if source.suffix.lower() == ".mp4" else "image/png" if source.suffix.lower() == ".png" else "image/jpeg"
            # --upload-file streams; --data-binary @file would buffer a large
            # hour-long MP4 in curl memory before the transfer starts.
            result = self.curl(["--request", "POST", "--upload-file", str(source),
                                "--header", "Content-Type: " + content_type, "--header", "Accept: application/json",
                                "--header", f"Content-SHA256: {record['sha256']}", "--header", f"Content-Length: {record['size']}",
                                "--output", str(response), "--write-out", "%{http_code}", f"{BASE}/{bin_id}/{name}"])
            if result.returncode or result.stdout.strip() != b"201":
                raise DeliveryError("Filebin did not acknowledge the upload; reconcile its listing before retrying.")
            data = read_json(response)
            item = data.get("file", {})
            if item.get("filename") != name or item.get("sha256") != record["sha256"] or item.get("bytes") != record["size"]:
                raise DeliveryError("Filebin's upload receipt does not match the selected bytes.")

    def verify_download(self, bin_id: str, name: str, record: dict) -> None:
        # A normal GET, never hundreds of byte-range requests. Retain no second
        # long-video copy after the single complete SHA-256 check.
        with tempfile.TemporaryDirectory(prefix="filebin-check-") as directory:
            root = Path(directory)
            cookies, headers, body = (root / name for name in ("cookies", "headers", "body"))
            for _ in range(3):  # Filebin's first-visit HTML cookie page is not a media download.
                result = self.curl(["--location", "--cookie", str(cookies), "--cookie-jar", str(cookies),
                                    "--max-filesize", str(max(record["size"], 1024 * 1024)),
                                    "--dump-header", str(headers), "--output", str(body), "--write-out", "%{http_code}",
                                    f"{BASE}/{bin_id}/{name}"])
                if result.returncode:
                    raise DeliveryError("Complete download did not finish; no download proof was recorded.")
                if result.stdout.strip() == b"403":
                    raise DeliveryError("Filebin refused the download (HTTP 403). Stop retrying and inspect its access/download limit.")
                if result.stdout.strip() != b"200":
                    raise DeliveryError("Filebin did not return a complete HTTP 200 download.")
                blocks = [block for block in headers.read_text(errors="replace").replace("\r\n", "\n").split("\n\n") if block.startswith("HTTP/")]
                if blocks and re.search(r"(?im)^content-type:\s*text/html\b", blocks[-1]):
                    continue
                if body.stat().st_size != record["size"] or digest(body) != record["sha256"]:
                    raise DeliveryError("Complete downloaded bytes differ from the original.")
                return
        raise DeliveryError("Filebin's verification page did not return the requested file.")

    def lock(self, bin_id: str) -> None:
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{BASE}/{bin_id}", data=b"", method="PUT"), timeout=30) as response:
                if response.status != 200:
                    raise DeliveryError("Filebin did not confirm the read-only lock.")
        except OSError:
            raise DeliveryError("Could not lock the completed Filebin delivery; preserve state and retry later.") from None


def matching(listing: dict, name: str, expected: dict) -> bool:
    items = [item for item in listing.get("files", []) if item.get("filename") == name]
    if not items:
        return False
    if len(items) != 1 or items[0].get("sha256") != expected["sha256"] or items[0].get("bytes") != expected["size"]:
        raise DeliveryError(f"Hosted file differs; refusing to overwrite {name}.")
    return True


def deliver(bundle_path: Path, state_path: Path, backdrop: Path | None = None, approved: bool = False,
            check_local_only: bool = False, timeout: int = 900, state_sync_command: list[str] | None = None) -> dict:
    bundle, files, identity = build_manifest(bundle_path, backdrop)
    expected = {name: {"size": item["size"], "sha256": item["sha256"]} for name, item in files.items()}
    if check_local_only:
        return {"bundle_id": bundle["bundle_id"], "files": expected, "network_used": False}
    if not approved:
        raise DeliveryError("Filebin delivery requires the user's permission and --approved-filebin.")
    state_path = state_path.expanduser().resolve()
    if state_path == bundle_path.expanduser().resolve() or any(item.get("path") == str(state_path) for item in files.values()):
        raise DeliveryError("Delivery state must not replace an input file.")
    if not 30 <= timeout <= 3600:
        raise DeliveryError("Request timeout must be between 30 and 3600 seconds.")
    if state_sync_command is not None and (not isinstance(state_sync_command, list) or not state_sync_command
            or not all(isinstance(argument, str) and argument and "\x00" not in argument for argument in state_sync_command)):
        raise DeliveryError("State synchronization must be a nonempty JSON array of command arguments.")
    state = read_json(state_path) if state_path.exists() else {
        "schema_version": 1, "provider": "filebin.net", "bin": secrets.token_hex(16),
        "bundle_id": bundle["bundle_id"], "identity": identity, "files": {}, "expected_files": expected,
    }
    allowed_state = {"schema_version", "provider", "bin", "bundle_id", "identity", "files", "expected_files", "expires_at", "readonly", "status", "folder_url"}
    if (state.get("schema_version") != 1 or state.get("provider") != "filebin.net"
            or not isinstance(state.get("bin"), str) or not re.fullmatch(r"[a-f0-9]{32}", state["bin"])
            or state.get("identity") != identity or state.get("bundle_id") != bundle["bundle_id"]
            or state.get("expected_files") != expected or not isinstance(state.get("files"), dict)
            or not set(state["files"]).issubset(files) or not set(state).issubset(allowed_state)):
        raise DeliveryError("Retained delivery state or local bundle differs; preserve it and review before creating a new delivery.")
    for name, receipt in state["files"].items():
        if (not isinstance(receipt, dict) or any(receipt.get(key) != expected[name][key] for key in ("size", "sha256"))
                or receipt.get("url") != f"{BASE}/{state['bin']}/{name}"
                or not set(receipt).issubset({"size", "sha256", "url", "uploaded", "download_verified", "download_verification_method"})
                or any(flag in receipt and type(receipt[flag]) is not bool for flag in ("uploaded", "download_verified"))
                or (receipt.get("download_verified") and (receipt.get("uploaded") is not True
                    or receipt.get("download_verification_method") != "one complete HTTP GET, SHA-256 and byte count"))):
            raise DeliveryError("Retained file receipt differs; no upload was started.")
    save_state(state_path, state, state_sync_command)  # Durable bin/allowlist before first request.
    client = FilebinClient(timeout)
    listing = client.listing(state["bin"])
    if any(item.get("filename") not in files for item in listing.get("files", [])):
        raise DeliveryError("The retained bin contains unexpected files; it was not modified.")
    for name, record in files.items():
        receipt = state["files"].setdefault(name, {**expected[name], "url": f"{BASE}/{state['bin']}/{name}"})
        if not matching(listing, name, record):
            if receipt.get("uploaded") or listing.get("bin", {}).get("readonly"):
                raise DeliveryError("A previously uploaded file is missing or expired; preserve its delivery record before making a new handoff.")
            save_state(state_path, state, state_sync_command)
            print("Uploading " + name, flush=True)
            try:
                client.upload(state["bin"], name, record)
            except DeliveryError:
                listing = client.listing(state["bin"])
                if not matching(listing, name, record):
                    raise
            listing = client.listing(state["bin"])
            if not matching(listing, name, record):
                raise DeliveryError("Uploaded file is missing from Filebin's listing.")
        receipt["uploaded"] = True
        state["expires_at"] = listing.get("bin", {}).get("expired_at")
        save_state(state_path, state, state_sync_command)
        if not receipt.get("download_verified"):
            print("Checking one complete download: " + name, flush=True)
            client.verify_download(state["bin"], name, record)
            receipt["download_verified"] = True
            receipt["download_verification_method"] = "one complete HTTP GET, SHA-256 and byte count"
            save_state(state_path, state, state_sync_command)
    if not listing.get("bin", {}).get("readonly"):
        client.lock(state["bin"])
    listing = client.listing(state["bin"])
    if (listing.get("bin", {}).get("readonly") is not True
            or any(not matching(listing, name, record) for name, record in files.items())
            or {item.get("filename") for item in listing.get("files", [])} != set(files)):
        raise DeliveryError("Final locked-folder verification failed; retained receipts remain available.")
    state.update({"readonly": True, "status": "delivered", "folder_url": f"{BASE}/{state['bin']}",
                  "expires_at": listing["bin"].get("expired_at")})
    if not state["expires_at"]:
        raise DeliveryError("Filebin did not report an expiry for this temporary delivery.")
    save_state(state_path, state, state_sync_command)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--backdrop", type=Path, help="explicit optional backdrop image to include")
    parser.add_argument("--approved-filebin", action="store_true", help="use only when the user authorized this delivery")
    parser.add_argument("--check-local-only", action="store_true", help="validate exact allowlist without network or state changes")
    parser.add_argument("--timeout", type=int, default=900, help="hard wall-clock limit for each upload/download request")
    parser.add_argument("--state-sync-command", help="JSON command-prefix argv; the saved state path is appended after every atomic save; failure stops subsequent uploads")
    args = parser.parse_args()
    try:
        try:
            sync_command = json.loads(args.state_sync_command) if args.state_sync_command else None
        except ValueError:
            raise DeliveryError("--state-sync-command must be a JSON argv array.") from None
        state = deliver(args.bundle, args.state, args.backdrop, args.approved_filebin, args.check_local_only, args.timeout, sync_command)
        print(json.dumps(state, indent=2))
    except (DeliveryError, OSError) as exc:
        print("Error: " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
