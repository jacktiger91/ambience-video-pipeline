#!/usr/bin/env python3
"""Deliver a prepared daily package using an access-controlled GitHub draft.

No generation, rendering, YouTube publication, email, or public Filebin URL
posting occurs here. GitHub's authenticated draft-release API retains prepared
archives and immutable receipt versions outside Git history.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from cloud_ledger import GitHubLedger, LedgerError
import daily_schedule
from encrypted_queue import QueueError, restore_private_bundle
import filebin_delivery
from filebin_cloud_state import merge, validate
from scheduled_bootstrap import BootstrapError, bootstrap_episode, bootstrap_probe


class RunnerError(Exception):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise RunnerError("A private-asset redirect attempted to leave HTTPS.")
        redirected = super().redirect_request(request, response, code, message, headers, url)
        if redirected is not None and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class DraftStore:
    def __init__(self, config: dict, token: str):
        self.api = GitHubLedger(config.get("repository", ""), token)
        self.release_id = config.get("private_release_id")
        self.tag = config.get("private_release_tag")
        if type(self.release_id) is not int or self.release_id <= 0 or not isinstance(self.tag, str) or not self.tag:
            raise RunnerError("Configure the dedicated private draft release before running delivery.")
        self.repository = config["repository"]
        self.opener = urllib.request.build_opener(SafeRedirect())

    def guard(self) -> dict:
        status, release = self.api.request("GET", f"/releases/{self.release_id}")
        if (status != 200 or not isinstance(release, dict) or release.get("draft") is not True
                or release.get("id") != self.release_id or release.get("tag_name") != self.tag):
            raise RunnerError("The configured release is not the expected private draft; no delivery write may proceed.")
        return release

    def anonymous_probe(self) -> None:
        self.guard()
        request = urllib.request.Request(self.api.root + f"/releases/{self.release_id}", headers={
            "Accept": "application/vnd.github+json", "User-Agent": "quiet-hours-privacy-preflight"})
        try:
            with urllib.request.urlopen(request, timeout=30):
                raise RunnerError("Anonymous access unexpectedly succeeded; do not store review files in this release.")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RunnerError("Anonymous draft access did not return the required HTTP 404; privacy is unverified.") from None
        except OSError:
            raise RunnerError("Anonymous access check could not complete; privacy is unverified.") from None

    def assets(self) -> list[dict]:
        self.guard()
        result = []
        for page in range(1, 12):
            status, items = self.api.request("GET", f"/releases/{self.release_id}/assets?per_page=100&page={page}")
            if status != 200 or not isinstance(items, list):
                raise RunnerError("Could not list the private queue assets.")
            result.extend(items)
            if len(items) < 100:
                return result
        raise RunnerError("Private queue asset limit reached; archive completed history before extending the queue.")

    def find_asset(self, asset_id: int) -> dict:
        if type(asset_id) is not int or asset_id <= 0:
            raise RunnerError("An exact private release asset ID is required.")
        found = [asset for asset in self.assets() if asset.get("id") == asset_id]
        if len(found) != 1 or found[0].get("state") != "uploaded":
            raise RunnerError("The prepared asset is absent or incomplete in this private draft.")
        return found[0]

    def download(self, asset: dict, destination: Path, expected_sha: str | None = None, expected_size: int | None = None) -> None:
        self.guard()
        size = asset.get("size")
        if type(size) is not int or not 0 < size < 2 * 1024**3 or (expected_size is not None and size != expected_size):
            raise RunnerError("Private asset size differs or exceeds the supported release-file limit.")
        if destination.exists():
            if expected_sha and destination.stat().st_size == size and filebin_delivery.digest(destination) == expected_sha:
                return
            raise RunnerError("Refusing to overwrite a different restored asset.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(self.api.root + f"/releases/assets/{asset['id']}", headers={
            "Authorization": "Bearer " + self.api.token, "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "quiet-hours-private-queue"})
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".private-", delete=False) as target:
                temporary = Path(target.name)
                started, count, observed = time.monotonic(), 0, hashlib.sha256()
                with self.opener.open(request, timeout=45) as response:
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > size or time.monotonic() - started > 1200:
                            raise RunnerError("Private asset exceeded its declared size or download deadline.")
                        target.write(chunk)
                        observed.update(chunk)
                if count != size or (expected_sha and observed.hexdigest() != expected_sha):
                    raise RunnerError("Downloaded private asset failed its size/SHA-256 check.")
            temporary.replace(destination)
        except (OSError, urllib.error.URLError):
            raise RunnerError("Private asset download failed; no signed URL or credential was logged.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def latest_receipt(self, bundle_id: str) -> tuple[dict | None, int]:
        pattern = re.compile(r"receipt-" + re.escape(bundle_id) + r"-(\d{6})\.json$")
        candidates = [(int(match.group(1)), item) for item in self.assets()
                      if (match := pattern.fullmatch(item.get("name", "")))]
        if not candidates:
            return None, 0
        version, asset = max(candidates, key=lambda item: item[0])
        if asset.get("state") != "uploaded" or not 0 < asset.get("size", 0) <= 1024 * 1024:
            raise RunnerError("Latest private receipt is incomplete; preserve it before attempting recovery.")
        with tempfile.TemporaryDirectory(prefix="receipt-read-") as temporary:
            path = Path(temporary) / "receipt.json"
            self.download(asset, path)
            receipt = validate(daily_schedule.read_json(path))
        if receipt["bundle_id"] != bundle_id:
            raise RunnerError("Private receipt identifies a different concept.")
        return receipt, version

    def save_receipt(self, local: dict) -> dict:
        validate(local)
        remote, version = self.latest_receipt(local["bundle_id"])
        value = merge(remote, local) if remote else local
        if remote == value:
            return value
        self.guard()
        name = f"receipt-{local['bundle_id']}-{version+1:06d}.json"
        body = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        if len(body) > 1024 * 1024:
            raise RunnerError("Private receipt exceeds its bounded metadata size.")
        url = f"https://uploads.github.com/repos/{self.repository}/releases/{self.release_id}/assets?" + urllib.parse.urlencode({"name": name})
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.api.token, "Content-Type": "application/json",
            "Accept": "application/vnd.github+json", "User-Agent": "quiet-hours-private-queue"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 201:
                    raise RunnerError("Private receipt save was not acknowledged.")
                asset = json.load(response)
        except (OSError, ValueError):
            raise RunnerError("Private receipt save failed; no further Filebin upload may run.") from None
        if asset.get("name") != name or asset.get("size") != len(body) or asset.get("state") != "uploaded":
            raise RunnerError("Private receipt upload acknowledgement differs from the intended bytes.")
        with tempfile.TemporaryDirectory(prefix="receipt-confirm-") as temporary:
            path = Path(temporary) / "receipt.json"
            self.download(asset, path, hashlib.sha256(body).hexdigest(), len(body))
        return value

    def publish_private_review(self, receipt: dict) -> str:
        validate(receipt)
        if receipt.get("status") != "delivered":
            raise RunnerError("An incomplete delivery cannot be listed as ready for review.")
        release = self.guard()
        bundle_id = receipt["bundle_id"]
        start, end = f"<!-- quiet-hours:{bundle_id}:start -->", f"<!-- quiet-hours:{bundle_id}:end -->"
        lines = [start, f"## {bundle_id}", "", f"[Download this concept's complete Filebin package]({receipt['folder_url']})",
                 "", "Link expiry (UTC): " + receipt["expires_at"], "",
                 "Review the MP4s and upload details. YouTube upload and publication remain manual.", end]
        section = "\n".join(lines)
        body = release.get("body") or "# Quiet Hours private review\n\nKeep this release as a draft. Publishing it would expose its assets and review links."
        if (start in body) != (end in body):
            raise RunnerError("Private review page contains an incomplete managed section.")
        if start in body:
            body = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: section, body, flags=re.S)
        else:
            body += "\n\n" + section
        status, updated = self.api.request("PATCH", f"/releases/{self.release_id}",
                                           {"body": body, "draft": True, "tag_name": self.tag})
        if (status != 200 or not isinstance(updated, dict) or updated.get("draft") is not True
                or updated.get("id") != self.release_id or updated.get("tag_name") != self.tag):
            raise RunnerError("Could not confirm that the review page remains a private draft.")
        # Draft-specific untagged URLs can change after an API update. The
        # authenticated Releases page remains a stable bookmark for the owner.
        return f"https://github.com/{self.repository}/releases"


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not value:
        raise RunnerError("A protected GitHub runtime credential is required.")
    return value


def probe_connection(config: dict, store: DraftStore, work: Path) -> dict:
    specification = config.get("connection_probe")
    if not isinstance(specification, dict):
        raise RunnerError("Configure an already-approved publishing-copy asset for the small connection test.")
    if (type(specification.get("size")) is not int or not 0 < specification["size"] <= 128 * 1024
            or not isinstance(specification.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", specification["sha256"])):
        raise RunnerError("The approved connection-test text needs its exact size and SHA-256.")
    asset = store.find_asset(specification.get("asset_id"))
    source = work / "approved-copy.txt"
    store.download(asset, source, specification.get("sha256"), specification.get("size"))
    if not 0 < source.stat().st_size <= 128 * 1024:
        raise RunnerError("The connection test is limited to approved publishing text.")
    identity = filebin_delivery.digest(source)
    bundle_id, name = "connection-probe", "connection-probe-upload-details.txt"
    expected = {name: {"size": source.stat().st_size, "sha256": identity}}
    state, _ = store.latest_receipt(bundle_id)
    if state and (state["identity"] != identity or state["expected_files"] != expected):
        raise RunnerError("The existing connection test used different copy; preserve its result.")
    state = state or {"schema_version": 1, "provider": "filebin.net", "bin": secrets.token_hex(16),
                      "bundle_id": bundle_id, "identity": identity, "expected_files": expected, "files": {}}
    state = store.save_receipt(state)
    client = filebin_delivery.FilebinClient(timeout=180)
    remote = client.listing(state["bin"])
    if any(item.get("filename") != name for item in remote.get("files", [])):
        raise RunnerError("The connection-test folder contains unexpected files.")
    record = {**expected[name], "path": str(source)}
    if not filebin_delivery.matching(remote, name, record):
        if state["files"].get(name, {}).get("uploaded") or remote.get("bin", {}).get("readonly"):
            raise RunnerError("The previous connection-test download expired or disappeared; do not silently recreate it.")
        client.upload(state["bin"], name, record)
        remote = client.listing(state["bin"])
        if not filebin_delivery.matching(remote, name, record):
            raise RunnerError("Connection-test upload is absent from the server listing.")
    info = state["files"].setdefault(name, {**expected[name], "url": f"https://filebin.net/{state['bin']}/{name}"})
    info["uploaded"] = True
    state = store.save_receipt(state)
    if not state["files"][name].get("download_verified"):
        client.verify_download(state["bin"], name, record)
        state["files"][name].update({"download_verified": True,
            "download_verification_method": "one complete HTTP GET, SHA-256 and byte count"})
        state = store.save_receipt(state)
    if not remote.get("bin", {}).get("readonly"):
        client.lock(state["bin"])
    remote = client.listing(state["bin"])
    if remote.get("bin", {}).get("readonly") is not True or not filebin_delivery.matching(remote, name, record):
        raise RunnerError("Connection-test final lock/check failed.")
    state.update({"status": "delivered", "readonly": True, "folder_url": "https://filebin.net/" + state["bin"],
                  "expires_at": remote["bin"]["expired_at"]})
    store.save_receipt(state)
    return {"status": "connection_test_passed", "private_draft_anonymous_http": 404,
            "full_text_download_verified": True, "media_delivered": False}


def run(config_path: Path, work: Path, mode: str, selected_date: str | None = None) -> dict:
    config_path = config_path.expanduser().resolve()
    config = daily_schedule.read_json(config_path)
    if mode == "scheduled" and selected_date:
        raise RunnerError("Scheduled delivery cannot select a different date.")
    if mode == "delivery-test" and not selected_date:
        raise RunnerError("A manual delivery test requires its explicit approved episode date.")
    day = date.fromisoformat(selected_date) if selected_date else None
    slot = daily_schedule.select_slot(config_path, episode_date=day)
    public = {key: slot[key] for key in ("episode_date", "ready_by", "ready_by_utc", "deadline_passed")}
    if mode == "scheduled" and (not slot["enabled"] or not slot["window_open"] or slot["blockers"]):
        return {**public, "status": "not_due_or_disabled", "reasons": slot["blockers"]}
    store = DraftStore(config, token())
    work = work.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    if mode in {"preflight", "connection-test", "bootstrap-probe", "bootstrap-episode", "delivery-test"}:
        store.anonymous_probe()
    elif config.get("draft_privacy_verified") is not True:
        raise RunnerError("Run the private-draft preflight before enabling automatic delivery.")
    if mode == "connection-test":
        return probe_connection(config, store, work)
    if mode == "bootstrap-probe":
        return bootstrap_probe(store, work)
    if mode == "bootstrap-episode":
        return bootstrap_episode(store, work)
    entry = config["prepared_episodes"].get(slot["episode_date"])
    if not isinstance(entry, dict) or entry.get("bundle_id") != slot.get("bundle_id"):
        raise RunnerError("Today's concept has no prepared private archive. No old concept was substituted.")
    bundle_id = entry["bundle_id"]
    expected_identity = entry.get("delivery_identity", "")
    if not isinstance(expected_identity, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_identity):
        raise RunnerError("Prepared queue entry lacks its delivery identity.")
    previous, _ = store.latest_receipt(bundle_id)
    if previous and previous["identity"] != expected_identity:
        raise RunnerError("Today's existing delivery contains different bytes; preserve it before review.")
    if previous and previous.get("status") == "delivered":
        required_until = max(datetime.now(timezone.utc), daily_schedule.aware_time(slot["ready_by"]))
        if daily_schedule.aware_time(previous.get("expires_at", "")) <= required_until:
            raise RunnerError("The existing Filebin delivery expires before the required review deadline; preserve its receipt and prepare an explicit replacement.")
        if mode in {"scheduled", "delivery-test"}:
            page = store.publish_private_review(previous)
            return {**public, "status": "already_delivered", "bundle_id": bundle_id, "review_page": page,
                    "expires_at": previous["expires_at"]}
    size, sha = entry.get("asset_size"), entry.get("asset_sha256", "")
    if type(size) is not int or not 0 < size < 2*1024**3 or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise RunnerError("Prepared archive needs its exact size and SHA-256.")
    source = work / "prepared.tar"
    store.download(store.find_asset(entry.get("asset_id")), source, sha, size)
    restored = restore_private_bundle(source, work / "restored", bundle_id=bundle_id)
    if restored["delivery_identity"] != expected_identity:
        raise RunnerError("Restored package differs from the prepared queue identity.")
    bundle, _, _ = filebin_delivery.build_manifest(restored["bundle_path"], restored["backdrop_path"])
    daily_schedule.check_motion_review(restored["motion_report_path"], bundle)
    if mode == "preflight":
        return {**public, "status": "preflight_passed", "bundle_id": bundle_id,
                "private_draft_anonymous_http": 404, "prepared_archive_verified": True,
                "all_three_motion_reviews_verified": True, "media_delivered": False}
    receipt_path = work / "delivery.json"
    if previous:
        filebin_delivery.save_state(receipt_path, previous)
    hook = [sys.executable, str(Path(__file__).resolve()), "sync-state", "--config", str(config_path)]
    with contextlib.redirect_stdout(sys.stderr):
        receipt = filebin_delivery.deliver(restored["bundle_path"], receipt_path, restored["backdrop_path"],
                                          approved=True, state_sync_command=hook)
    page = store.publish_private_review(receipt)
    complete = datetime.now(timezone.utc)
    return {**public, "status": "delivered", "bundle_id": bundle_id, "review_page": page,
            "expires_at": receipt["expires_at"], "completed_at": complete.isoformat(),
            "completed_by_deadline": complete <= daily_schedule.aware_time(slot["ready_by"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    job = commands.add_parser("run")
    job.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config/scheduled-delivery.json")
    job.add_argument("--work-dir", type=Path, required=True)
    job.add_argument("--mode", choices=("scheduled", "preflight", "connection-test", "bootstrap-probe", "bootstrap-episode", "delivery-test"), default="preflight")
    job.add_argument("--date", help="explicit manual preflight/delivery-test date; scheduled runs always use today's Cyprus date")
    sync = commands.add_parser("sync-state")
    sync.add_argument("--config", type=Path, required=True)
    sync.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "sync-state":
            config = daily_schedule.read_json(args.config)
            receipt = DraftStore(config, token()).save_receipt(validate(daily_schedule.read_json(args.path)))
            filebin_delivery.save_state(args.path, receipt)
            print("Private delivery receipt saved.", file=sys.stderr)
            return 0
        result = run(args.config, args.work_dir, args.mode, args.date)
        print(json.dumps(result, indent=2))
        return 0
    except (RunnerError, BootstrapError, QueueError, daily_schedule.ScheduleError, filebin_delivery.DeliveryError, LedgerError, OSError, ValueError) as exc:
        # All lower-level network errors are converted to messages that omit
        # signed download URLs, private release bodies and credential values.
        print("Delivery stopped: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
