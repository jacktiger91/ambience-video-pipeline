#!/usr/bin/env python3
"""Stage verified bundles as unlisted YouTube uploads, with durable retry records.

The private journal contains resumable session URLs. The optional remote ledger
contains only hashes, attempt markers and video IDs and can safely survive a new
cloud runner. Neither this module nor its CLI can publish a video.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3/"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/"
CHUNK_SIZE = 8 * 1024 * 1024  # Resumable chunks must be multiples of 256 KiB.
KEYS = ("long", "short1", "short2")


class YouTubeError(Exception):
    """A diagnostic that is safe to display without credentials or session URLs."""


class APIError(YouTubeError):
    def __init__(self, status: int, reason: str = "request_failed"):
        self.status = status
        super().__init__(f"YouTube request failed (HTTP {status}, {reason}).")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeError) as exc:
            raise YouTubeError("YouTube returned an unreadable response; preserve the upload ledger.") from exc
        if not isinstance(value, dict):
            raise YouTubeError("YouTube returned unexpected response metadata.")
        return value


class Client:
    def __init__(self, access_token: str, refresh=None):
        self.access_token = access_token
        self.refresh = refresh
        # Never forward an Authorization header to a redirected host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, url: str, body: bytes | None = None,
                headers: dict[str, str] | None = None, *, retry: bool = False) -> Response:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "youtube.googleapis.com"}:
            raise YouTubeError("Refusing an unexpected YouTube API/session destination.")
        request_headers = {"Authorization": f"Bearer {self.access_token}", **(headers or {})}
        for attempt in range(5):
            request_headers["Authorization"] = f"Bearer {self.access_token}"
            req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                with self.opener.open(req, timeout=120) as result:
                    return Response(result.status, {k.lower(): v for k, v in result.headers.items()}, result.read())
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                if exc.code == 308:
                    return Response(308, {k.lower(): v for k, v in exc.headers.items()}, payload)
                if exc.code == 401 and self.refresh is not None and attempt == 0:
                    self.access_token = self.refresh()
                    continue
                if retry and exc.code in {429, 500, 502, 503, 504} and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                reason = "request_failed"
                try:
                    candidate = json.loads(payload)["error"]["errors"][0]["reason"]
                    if re.fullmatch(r"[A-Za-z0-9_]+", candidate):
                        reason = candidate
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
                raise APIError(exc.code, reason) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if retry and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise YouTubeError("YouTube network request failed; retain the journal and retry recovery.") from None
        raise YouTubeError("YouTube request did not complete.")

    def api(self, method: str, resource: str, params: dict[str, Any], body=None) -> dict[str, Any]:
        url = API + resource + "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode()
        return self.request(method, url, data, {"Content-Type": "application/json"}, retry=method == "GET").json()


def atomic_json(path: Path, value: dict[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600 if private else 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def marker(bundle_id: str, key: str) -> str:
    return "ambience-job-" + hashlib.sha256(f"{bundle_id}:{key}".encode()).hexdigest()[:32]


def validate_bundle(path: Path) -> dict[str, Any]:
    try:
        bundle = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise YouTubeError("Cannot read bundle manifest.") from exc
    if bundle.get("schema_version") != 1 or not isinstance(bundle.get("bundle_id"), str) or not bundle["bundle_id"]:
        raise YouTubeError("Bundle must have schema_version 1 and a stable bundle_id.")
    if bundle.get("schedule_enabled", False) is not False:
        raise YouTubeError("Test bundles must leave the daily schedule disabled.")
    videos = bundle.get("videos", [])
    if len(videos) != 3 or {item.get("key") for item in videos} != set(KEYS):
        raise YouTubeError("Bundle must contain exactly long, short1, and short2.")
    if not str(bundle.get("recording_credits", "")).strip():
        raise YouTubeError("Recording credits must be supplied before upload.")
    for item in videos:
        for field in ("title", "description"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise YouTubeError(f"{item['key']} needs a finished {field}.")
        if len(item["title"]) > 100 or len(item["description"]) > 5000:
            raise YouTubeError(f"{item['key']} title or description exceeds YouTube's limit.")
        if any(type(item.get(field)) is not bool for field in ("made_for_kids", "contains_synthetic_media")):
            raise YouTubeError("Each video needs explicit made_for_kids and contains_synthetic_media decisions.")
        source = Path(item.get("path", ""))
        if not source.is_absolute():
            source = path.parent / source
        if not source.is_file() or not item.get("sha256") or file_sha256(source) != item["sha256"]:
            raise YouTubeError(f"{item['key']} file is missing or differs from its verified hash.")
        item["path"] = str(source.resolve())
        verification = item.get("verification", {})
        if verification.get("full_decode") is not True:
            raise YouTubeError(f"{item['key']} has not passed a full-playthrough decode check.")
        expected_size = [1920, 1080] if item["key"] == "long" else [1080, 1920]
        if verification.get("resolution") != expected_size:
            raise YouTubeError(f"{item['key']} has the wrong verified resolution.")
        duration = float(verification.get("duration", 0))
        if (item["key"] == "long" and abs(duration - 3600) > 0.1) or (item["key"] != "long" and not 14.9 <= duration <= 30.1):
            raise YouTubeError(f"{item['key']} has the wrong verified duration.")
        if item.get("thumbnail"):
            thumbnail = Path(item["thumbnail"])
            if not thumbnail.is_absolute():
                thumbnail = path.parent / thumbnail
            if not thumbnail.is_file() or thumbnail.stat().st_size > 2 * 1024 * 1024:
                raise YouTubeError("Long-video thumbnail must exist and be no larger than 2 MiB.")
            expected_hash = item.get("thumbnail_sha256")
            if expected_hash and file_sha256(thumbnail) != expected_hash:
                raise YouTubeError("Thumbnail differs from its verified hash.")
            item["thumbnail"] = str(thumbnail.resolve())
    return bundle


def list_owned_uploads(client: Client, channel: dict[str, Any]) -> list[dict[str, Any]]:
    playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    found: list[dict[str, Any]] = []
    page = None
    for _ in range(10000):
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if page:
            params["pageToken"] = page
        result = client.api("GET", "playlistItems", params)
        ids = [entry["contentDetails"]["videoId"] for entry in result.get("items", [])]
        if ids:
            found.extend(client.api("GET", "videos", {
                "part": "snippet,status,processingDetails", "id": ",".join(ids), "maxResults": 50,
            }).get("items", []))
        page = result.get("nextPageToken")
        if not page:
            return found
    raise YouTubeError("Upload history scan did not complete; refusing a potentially duplicate upload.")


def validate_state_paths(journal_path: Path, report_path: Path,
                         ledger_path: Path | None = None) -> tuple[Path, Path, Path | None]:
    """Keep sensitive session state outside Git and away from public reports."""
    journal = journal_path.expanduser().resolve()
    report = report_path.expanduser().resolve()
    ledger = ledger_path.expanduser().resolve() if ledger_path is not None else None
    checkout = Path(__file__).resolve().parent
    if journal.is_relative_to(checkout):
        raise YouTubeError("Store the private upload journal outside the repository; it can contain secret resumable-session URLs.")
    paths = [journal, report, journal.with_suffix(journal.suffix + ".lock")]
    if ledger is not None:
        paths.append(ledger)
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if first == second or (first.exists() and second.exists() and first.samefile(second)):
                raise YouTubeError("Private journal, review report, safe ledger and lock file must use distinct paths.")
    if journal.exists() and journal.stat().st_mode & 0o077:
        raise YouTubeError("Protect the private upload journal with chmod 600 before reading it.")
    return journal, report, ledger


class Stage:
    def __init__(self, client: Client, bundle: dict[str, Any], channel_id: str,
                 journal_path: Path, report_path: Path, *, ledger_path: Path | None = None,
                 sync_command: Path | None = None, existing: dict[str, str] | None = None,
                 apply_playlist: bool = False, scopes: set[str] | None = None,
                 connection_warning: str | None = None):
        journal_path, report_path, ledger_path = validate_state_paths(journal_path, report_path, ledger_path)
        self.client, self.bundle, self.channel_id = client, bundle, channel_id
        self.journal_path, self.report_path = journal_path, report_path
        self.ledger_path, self.sync_command = ledger_path, sync_command
        self.existing, self.apply_playlist, self.scopes = existing or {}, apply_playlist, scopes or set()
        self.warning = connection_warning
        initial = {"schema_version": 1, "bundle_id": bundle["bundle_id"], "intended_channel_id": channel_id, "videos": {}}
        self.journal = self.load_state(journal_path, initial)
        self.ledger = self.load_state(ledger_path, initial) if ledger_path else json.loads(json.dumps(initial))
        for key, record in self.ledger["videos"].items():
            local = self.journal["videos"].setdefault(key, {})
            if local.get("video_id") and record.get("video_id") and local["video_id"] != record["video_id"]:
                raise YouTubeError("Journal and remote ledger disagree on a video ID.")
            for field in ("sha256", "attempt_started", "video_id"):
                if field in record:
                    local.setdefault(field, record[field])
        self.results: dict[str, dict[str, Any]] = {}

    @staticmethod
    def load_state(path: Path | None, initial: dict[str, Any]) -> dict[str, Any]:
        if path is None or not path.exists():
            return json.loads(json.dumps(initial))
        try:
            value = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            raise YouTubeError("Cannot read upload state; preserve it and resolve the error before uploading.") from exc
        if any(value.get(k) != initial[k] for k in ("schema_version", "bundle_id", "intended_channel_id")):
            raise YouTubeError("Upload state belongs to another bundle or channel.")
        return value

    def save(self, *, sync: bool = False) -> None:
        atomic_json(self.journal_path, self.journal, private=True)
        self.ledger["videos"] = {
            key: {name: value for name, value in record.items() if name in {"sha256", "attempt_started", "video_id"}}
            for key, record in self.journal["videos"].items()
        }
        if self.ledger_path:
            atomic_json(self.ledger_path, self.ledger)
        self.write_report()
        if sync:
            if self.sync_command is not None:
                if self.ledger_path is None:
                    raise YouTubeError("A ledger sync command requires --ledger.")
                result = subprocess.run([str(self.sync_command.resolve()), str(self.ledger_path.resolve())], check=False)
                if result.returncode:
                    raise YouTubeError("Remote upload-ledger persistence failed. Stop and preserve local records before retrying.")
                # The sync helper may merge a video ID already saved by another
                # runner. Observe it before any subsequent upload initiation.
                merged = self.load_state(self.ledger_path, self.ledger)
                for key, incoming in merged["videos"].items():
                    local = self.journal["videos"].setdefault(key, {})
                    if local.get("sha256") and incoming.get("sha256") != local["sha256"]:
                        raise YouTubeError("Remote ledger media hash conflict; no new upload may be created.")
                    if local.get("video_id") and incoming.get("video_id") and local["video_id"] != incoming["video_id"]:
                        raise YouTubeError("Remote ledger video ID conflict; review Studio before proceeding.")
                    for field in ("sha256", "attempt_started", "video_id"):
                        if field in incoming:
                            local[field] = incoming[field]
                self.ledger = merged
                atomic_json(self.journal_path, self.journal, private=True)
                self.write_report()

    def write_report(self) -> None:
        videos = []
        for key in KEYS:
            record = self.journal["videos"].get(key, {})
            entry = {"key": key, "requested_visibility": "unlisted", "attempt_started": record.get("attempt_started", False)}
            video_id = record.get("video_id")
            if video_id:
                entry.update(video_id=video_id, studio_url=f"https://studio.youtube.com/video/{video_id}/edit",
                             watch_url=f"https://www.youtube.com/watch?v={video_id}")
            entry.update(self.results.get(key, {}))
            videos.append(entry)
        atomic_json(self.report_path, {
            "schema_version": 1, "bundle_id": self.bundle["bundle_id"], "channel_id": self.channel_id,
            "schedule_enabled": False, "connection_warning": self.warning,
            "daily_connection_ready": self.warning is None, "videos": videos,
            "review_order": "Review and publish the full video first, then the two Shorts.",
        })

    def record_id(self, key: str, video_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise YouTubeError("YouTube returned an invalid video ID; preserve the session journal.")
        record = self.journal["videos"][key]
        record["attempt_started"] = True
        record["video_id"] = video_id
        record.pop("session_url", None)
        self.save(sync=True)  # Persist ID before any follow-up request or next upload.

    def video_metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        description = item["description"]
        long_id = self.journal["videos"].get("long", {}).get("video_id")
        if item["key"] != "long" and long_id:
            link = f"https://www.youtube.com/watch?v={long_id}"
            if link not in description:
                description += "\n\nWatch the full hour: " + link
        if len(description) > 5000:
            raise YouTubeError("Short description has no space for the full-video link.")
        return {
            "snippet": {
                "title": item["title"], "description": description,
                "categoryId": str(item.get("category_id", "10")),
                "tags": [marker(self.bundle["bundle_id"], item["key"]), "ambience-sha256-" + item["sha256"]],
            },
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": item["made_for_kids"],
                       "containsSyntheticMedia": item["contains_synthetic_media"]},
        }

    def recover(self, item: dict[str, Any], uploads: list[dict[str, Any]]) -> str | None:
        key = item["key"]
        record = self.journal["videos"].setdefault(key, {"sha256": item["sha256"]})
        if record.get("sha256") != item["sha256"]:
            raise YouTubeError("Bundle media changed after upload was started; use a new bundle ID.")
        claimed = self.existing.get(key) or record.get("video_id")
        if claimed:
            data = self.client.api("GET", "videos", {"part": "snippet,status,processingDetails", "id": claimed})
            results = data.get("items", [])
            if len(results) != 1 or results[0].get("snippet", {}).get("channelId") != self.channel_id:
                raise YouTubeError("Existing video ID is unavailable or belongs to another channel.")
            self.record_id(key, claimed)
            return claimed
        expected = marker(self.bundle["bundle_id"], key)
        matches = [entry for entry in uploads if expected in entry.get("snippet", {}).get("tags", [])]
        if len(matches) > 1:
            raise YouTubeError("Multiple uploads have this job marker; review Studio before proceeding.")
        if matches:
            if "ambience-sha256-" + item["sha256"] not in matches[0].get("snippet", {}).get("tags", []):
                raise YouTubeError("Recovered job marker has a different or missing media hash; review the upload before explicitly reusing it.")
            self.record_id(key, matches[0]["id"])
            return matches[0]["id"]
        if record.get("attempt_started") and not record.get("session_url"):
            raise YouTubeError("An earlier upload attempt has no recovered ID/session. Wait for processing, inspect Studio, then pass --existing KEY=ID; no duplicate was created.")
        return None

    def upload(self, item: dict[str, Any]) -> str:
        key = item["key"]
        source = Path(item["path"])
        size = source.stat().st_size
        record = self.journal["videos"][key]
        if not record.get("session_url"):
            record["attempt_started"] = True
            self.save(sync=True)  # Must be durable BEFORE any request that creates a video.
            if record.get("video_id"):
                recovered = self.recover(item, [])
                if recovered:
                    return recovered
            response = self.client.request(
                "POST", UPLOAD + "videos?uploadType=resumable&part=snippet%2Cstatus",
                json.dumps(self.video_metadata(item)).encode(),
                {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(size),
                 "X-Upload-Content-Type": "video/mp4"},
            )
            session = response.headers.get("location")
            if not session:
                raise YouTubeError("YouTube did not return an upload session. Preserve the pending attempt ledger.")
            parsed = urllib.parse.urlsplit(session)
            if parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "youtube.googleapis.com"}:
                raise YouTubeError("YouTube returned an unexpected upload-session host.")
            record["session_url"] = session
            self.save()
        session = record["session_url"]
        # Always query server progress, including when resuming after interruption.
        response = self.client.request("PUT", session, b"", {"Content-Length": "0", "Content-Range": f"bytes */{size}"}, retry=True)
        failures = 0
        with source.open("rb") as handle:
            while True:
                if response.status in {200, 201}:
                    video_id = response.json().get("id", "")
                    self.record_id(key, video_id)
                    return video_id
                if response.status != 308:
                    raise YouTubeError("Unexpected resumable-upload response; retain the journal.")
                completed = response.headers.get("range")
                match = re.fullmatch(r"bytes=0-([0-9]+)", completed or "")
                if completed and match is None:
                    raise YouTubeError("Invalid resumable-upload range; refusing to guess progress.")
                offset = int(match.group(1)) + 1 if match else 0
                if offset >= size:
                    raise YouTubeError("All upload bytes were received but no ID returned. Preserve state and retry recovery later.")
                handle.seek(offset)
                data = handle.read(CHUNK_SIZE)
                try:
                    response = self.client.request(
                        "PUT", session, data,
                        {"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                         "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"},
                    )
                    failures = 0
                except YouTubeError as exc:
                    if isinstance(exc, APIError) and exc.status not in {401, 429, 500, 502, 503, 504}:
                        raise
                    failures += 1
                    if failures > 5:
                        raise
                    time.sleep(2 ** (failures - 1))
                    # A lost success response must never start another upload.
                    response = self.client.request("PUT", session, b"", {
                        "Content-Length": "0", "Content-Range": f"bytes */{size}"}, retry=True)

    def finish_settings(self, item: dict[str, Any], video_id: str, *, reused: bool) -> None:
        key = item["key"]
        result: dict[str, Any] = {"manual_settings": []}
        self.results[key] = result
        if reused:
            before = self.client.api("GET", "videos", {"part": "snippet,status", "id": video_id}).get("items", [])
            if not before:
                raise YouTubeError("Existing upload disappeared; its ID remains recorded.")
            if before[0].get("status", {}).get("privacyStatus") == "public":
                result["actual_visibility"] = "public"
                raise YouTubeError("Existing video is already public; review it in Studio instead of silently changing a published video.")
            body = {"id": video_id, **self.video_metadata(item)}
            try:
                self.client.api("PUT", "videos", {"part": "snippet,status"}, body)
            except YouTubeError as exc:
                result["manual_settings"].append(f"Apply updated title, description, unlisted visibility and disclosures in Studio: {exc}")
        if key == "long" and item.get("thumbnail"):
            thumbnail = Path(item["thumbnail"])
            content_type = "image/png" if thumbnail.suffix.lower() == ".png" else "image/jpeg"
            try:
                self.client.request("POST", UPLOAD + "thumbnails/set?" + urllib.parse.urlencode({"videoId": video_id, "uploadType": "media"}),
                                    thumbnail.read_bytes(), {"Content-Type": content_type}, retry=True)
                result["thumbnail_applied"] = True
            except YouTubeError as exc:
                result["manual_settings"].append(f"Set the supplied thumbnail in Studio (custom thumbnails may require channel verification): {exc}")
        if key == "long" and self.bundle.get("playlist_id"):
            if not self.apply_playlist:
                result["manual_settings"].append("Add the long video to playlist " + self.bundle["playlist_id"] + " in Studio (or explicitly grant playlist scope and use --apply-playlist).")
            else:
                from youtube_auth import PLAYLIST_SCOPE
                if PLAYLIST_SCOPE not in self.scopes:
                    raise YouTubeError("Playlist writes require an explicit --manage-playlist authorization; no extra scope was requested automatically.")
                try:
                    playlist_id = self.bundle["playlist_id"]
                    exists = self.client.api("GET", "playlistItems", {"part": "id", "playlistId": playlist_id, "videoId": video_id})
                    if not exists.get("items"):
                        self.client.api("POST", "playlistItems", {"part": "snippet"}, {
                            "snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}})
                    result["playlist_applied"] = True
                except YouTubeError as exc:
                    result["manual_settings"].append(f"Add the long video to the selected playlist in Studio: {exc}")
        if key != "long":
            long_id = self.journal["videos"].get("long", {}).get("video_id", "the full upload")
            result["manual_settings"].extend([
                f"Set Related video to {long_id} in Studio. YouTube Data API v3 does not expose this setting.",
                "Choose the supplied suggested cover frame using YouTube's supported Shorts cover editor; the API cannot select a Shorts cover frame.",
            ])
        info = self.client.api("GET", "videos", {"part": "snippet,status,processingDetails", "id": video_id}).get("items", [])
        if not info:
            raise YouTubeError("Uploaded ID is saved, but YouTube has not returned its status yet; retry status verification.")
        status = info[0].get("status", {})
        result["actual_visibility"] = status.get("privacyStatus", "unknown")
        result["processing_status"] = info[0].get("processingDetails", {}).get("processingStatus", status.get("uploadStatus", "unknown"))
        if result["actual_visibility"] != "unlisted":
            result["manual_settings"].append(
                "Unlisted was requested but YouTube returned " + result["actual_visibility"] + ". Open the Studio edit link and review Visibility. New/unverified API projects may be restricted to private uploads pending a YouTube API compliance audit; OAuth Production status does not remove that restriction."
            )
        if status.get("uploadStatus") in {"failed", "rejected", "deleted"}:
            result["manual_settings"].append("YouTube reports " + status["uploadStatus"] + "; inspect Studio before publishing.")
        self.save()

    def run(self) -> dict[str, Any]:
        channels = self.client.api("GET", "channels", {"part": "id,contentDetails", "mine": "true"}).get("items", [])
        matches = [channel for channel in channels if channel.get("id") == self.channel_id]
        if len(matches) != 1:
            raise YouTubeError("Authenticated channel does not match the intended channel; no uploads were started.")
        uploads = list_owned_uploads(self.client, matches[0])
        items = {item["key"]: item for item in self.bundle["videos"]}
        for key in KEYS:
            item = items[key]
            video_id = self.recover(item, uploads)
            reused = video_id is not None
            if video_id is None:
                video_id = self.upload(item)
            self.finish_settings(item, video_id, reused=reused)
        self.save()
        return json.loads(self.report_path.read_text())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage_parser = sub.add_parser("stage", help="stage three verified videos as unlisted; never publish")
    stage_parser.add_argument("--bundle", type=Path, required=True)
    stage_parser.add_argument("--journal", type=Path, required=True, help="protected private state; never put in Git or artifacts")
    stage_parser.add_argument("--review-report", type=Path, required=True, help="sanitized JSON with IDs, Studio links and settings")
    stage_parser.add_argument("--ledger", type=Path, help="nonsecret attempt/ID ledger restored before each cloud run")
    stage_parser.add_argument("--ledger-sync-command", type=Path, help="executable receiving ledger path; must durably save it or return nonzero")
    intended = stage_parser.add_mutually_exclusive_group(required=True)
    intended.add_argument("--expected-channel")
    intended.add_argument("--expected-handle")
    stage_parser.add_argument("--client-secret", type=Path)
    stage_parser.add_argument("--token-file", type=Path)
    stage_parser.add_argument("--existing", action="append", default=[], metavar="KEY=VIDEO_ID")
    stage_parser.add_argument("--apply-playlist", action="store_true")
    args = parser.parse_args(argv)
    lock = None
    stage = None
    try:
        import youtube_auth as auth
        args.journal, args.review_report, args.ledger = validate_state_paths(args.journal, args.review_report, args.ledger)
        bundle = validate_bundle(args.bundle)
        existing = {}
        for value in args.existing:
            key, separator, video_id = value.partition("=")
            if not separator or key not in KEYS or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                raise YouTubeError("--existing must use long=VIDEO_ID, short1=VIDEO_ID, or short2=VIDEO_ID.")
            existing[key] = video_id
        client_data, token_data = auth.load_credentials(args.client_secret, args.token_file)
        scopes = auth.scope_set(token_data)
        if not {auth.UPLOAD_SCOPE, auth.READONLY_SCOPE} <= scopes:
            raise YouTubeError("Channel verification and safe retry require youtube.readonly in addition to youtube.upload. Re-authorize explicitly with --verify-channel; no upload was attempted.")
        if args.apply_playlist and auth.PLAYLIST_SCOPE not in scopes:
            raise YouTubeError("--apply-playlist needs explicit --manage-playlist authorization.")
        refresh = lambda: auth.refresh_access_token(client_data, token_data)
        access = refresh()
        scopes = auth.scope_set(token_data)
        required_scopes = {auth.UPLOAD_SCOPE, auth.READONLY_SCOPE}
        if args.apply_playlist:
            required_scopes.add(auth.PLAYLIST_SCOPE)
        if not required_scopes <= scopes:
            raise YouTubeError("The refreshed grant lacks required permissions; explicitly authorize the required scopes again.")
        expected = auth.resolve_expected_channel(access, args.expected_channel, args.expected_handle)
        if bundle.get("intended_channel_id") and bundle["intended_channel_id"] != expected:
            raise YouTubeError("Bundle intended_channel_id differs from the requested channel.")
        if bundle.get("intended_channel_handle"):
            bundle_channel = auth.resolve_expected_channel(access, None, bundle["intended_channel_handle"])
            if bundle_channel != expected:
                raise YouTubeError("Bundle's intended handle resolves to a different channel.")
        args.journal.parent.mkdir(parents=True, exist_ok=True)
        lock_path = args.journal.with_suffix(args.journal.suffix + ".lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        lock = os.fdopen(lock_fd, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise YouTubeError("This upload journal is already in use by another process.") from exc
        stage = Stage(Client(access, refresh), bundle, expected, args.journal, args.review_report,
                      ledger_path=args.ledger, sync_command=args.ledger_sync_command, existing=existing,
                      apply_playlist=args.apply_playlist, scopes=scopes, connection_warning=auth.connection_warning(token_data))
        report = stage.run()
        for video in report["videos"]:
            print(f"{video['key']}: {video['studio_url']} — actual visibility: {video.get('actual_visibility', 'unknown')}")
        print(f"Review details: {args.review_report}. Schedule remains disabled.")
        if report.get("connection_warning"):
            print("Connection warning: " + report["connection_warning"], file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("Upload interrupted. Preserve the private journal and safe ledger; retry will recover progress.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never display raw HTTP exceptions: they may include a session URL.
        safe = isinstance(exc, (YouTubeError, OSError)) or type(exc).__name__ == "AuthError"
        message = str(exc) if safe else "Upload setup failed; inspect the local configuration without exposing credentials."
        print("Error: " + message, file=sys.stderr)
        if stage is not None:
            stage.write_report()
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
