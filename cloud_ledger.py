#!/usr/bin/env python3
"""Persist only nonsecret upload intents and IDs on a separate GitHub branch.

Invoked as the uploader's sync executable with a single ledger path, or with
``pull --output PATH --bundle-id ID`` before a cloud run. OAuth tokens and
resumable upload URLs are deliberately not accepted by this schema.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class LedgerError(Exception):
    pass


def validate(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"schema_version", "bundle_id", "intended_channel_id", "videos"}:
        raise LedgerError("Ledger has unexpected fields; only nonsecret intent and video ID fields are permitted.")
    if data["schema_version"] != 1 or not isinstance(data["bundle_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", data["bundle_id"]):
        raise LedgerError("Invalid ledger version or bundle ID.")
    if not isinstance(data["intended_channel_id"], str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", data["intended_channel_id"]):
        raise LedgerError("A verified intended channel ID is required before saving a cloud ledger.")
    if not isinstance(data["videos"], dict) or set(data["videos"]) - {"long", "short1", "short2"}:
        raise LedgerError("Invalid ledger video keys.")
    for record in data["videos"].values():
        if not isinstance(record, dict) or set(record) - {"sha256", "attempt_started", "video_id"}:
            raise LedgerError("Unexpected video fields; upload session URLs and credentials must never enter the ledger.")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"]):
            raise LedgerError("Invalid media SHA256 in ledger.")
        if not isinstance(record.get("attempt_started"), bool):
            raise LedgerError("Ledger attempt_started must be a boolean.")
        if record.get("video_id") is not None and (
            not isinstance(record["video_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", record["video_id"])
        ):
            raise LedgerError("Invalid YouTube video ID in ledger.")
    return data


def merge(remote: dict, local: dict) -> dict:
    validate(remote)
    validate(local)
    if any(remote[k] != local[k] for k in ("schema_version", "bundle_id", "intended_channel_id")):
        raise LedgerError("Cloud ledger identity conflicts with this run; refusing to upload.")
    result = json.loads(json.dumps(remote))
    for key, item in local["videos"].items():
        existing = result["videos"].get(key)
        if existing:
            if existing["sha256"] != item["sha256"]:
                raise LedgerError(f"Cloud ledger media changed for {key}; use a new bundle ID.")
            if existing.get("video_id") and item.get("video_id") and existing["video_id"] != item["video_id"]:
                raise LedgerError(f"Conflicting uploaded video IDs for {key}; manual resolution required.")
            result["videos"][key] = {
                "sha256": item["sha256"],
                "attempt_started": existing["attempt_started"] or item["attempt_started"],
            }
            video_id = existing.get("video_id") or item.get("video_id")
            if video_id:
                result["videos"][key]["video_id"] = video_id
        else:
            result["videos"][key] = dict(item)
    return result


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class GitHubLedger:
    def __init__(self, repository: str, token: str, branch: str = "ambience-upload-state"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise LedgerError("GITHUB_REPOSITORY must be owner/repository.")
        if not token:
            raise LedgerError("GITHUB_TOKEN is required for durable cloud upload records.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", branch):
            raise LedgerError("Invalid upload ledger branch.")
        self.root = "https://api.github.com/repos/" + repository
        self.token = token
        self.branch = branch

    def request(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode()
        for attempt in range(3):
            request = urllib.request.Request(self.root + path, data=body, method=method, headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "quiet-hours-cloud-ledger",
            })
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 409, 422):
                    return exc.code, None
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise LedgerError(f"GitHub ledger request failed (HTTP {exc.code}); no upload may proceed without durable state.") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise LedgerError("GitHub ledger network request failed; no upload may proceed without durable state.") from None
            time.sleep(2 ** attempt)
        raise LedgerError("GitHub ledger request failed.")

    def ensure_branch(self) -> None:
        status, _ = self.request("GET", "/git/ref/heads/" + self.branch)
        if status == 200:
            return
        status, repository = self.request("GET", "")
        if status != 200:
            raise LedgerError("Cannot access the repository for cloud ledger storage.")
        default = urllib.parse.quote(repository["default_branch"], safe="")
        status, ref = self.request("GET", "/git/ref/heads/" + default)
        if status != 200:
            raise LedgerError("Push the application code first; cloud ledger requires an existing repository commit.")
        status, _ = self.request("POST", "/git/refs", {"ref": "refs/heads/" + self.branch, "sha": ref["object"]["sha"]})
        if status == 422:
            status, _ = self.request("GET", "/git/ref/heads/" + self.branch)
        if status not in (200, 201):
            raise LedgerError("Cannot create the cloud ledger branch; check repository permissions.")

    def read(self, bundle_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", bundle_id):
            raise LedgerError("Invalid bundle ID.")
        status, data = self.request("GET", f"/contents/upload-state/{bundle_id}.json?ref={self.branch}")
        if status == 404:
            # Prove the repository itself is reachable before treating 404 as
            # the absence of a record (private access failures also return 404).
            status, _ = self.request("GET", "")
            if status != 200:
                raise LedgerError("Cannot read cloud ledger repository; absence of state is unconfirmed.")
            return None, None
        if status != 200 or data.get("encoding") != "base64":
            raise LedgerError("Cannot read the existing cloud ledger.")
        try:
            ledger = validate(json.loads(base64.b64decode(data["content"])))
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerError("Existing cloud ledger is malformed; refusing to replace it.") from exc
        if ledger["bundle_id"] != bundle_id:
            raise LedgerError("Cloud ledger bundle identity mismatch.")
        return ledger, data["sha"]

    def push(self, local: dict) -> dict:
        validate(local)
        self.ensure_branch()
        for _ in range(3):
            remote, sha = self.read(local["bundle_id"])
            merged = merge(remote, local) if remote else local
            payload = {
                "message": "Record ambience upload state: " + local["bundle_id"],
                "branch": self.branch,
                "content": base64.b64encode((json.dumps(merged, indent=2) + "\n").encode()).decode(),
            }
            if sha:
                payload["sha"] = sha
            status, _ = self.request("PUT", f"/contents/upload-state/{local['bundle_id']}.json", payload)
            if status in (200, 201):
                return merged
            if status not in (409, 422):
                break
        raise LedgerError("Cloud ledger update could not be confirmed; upload must stop.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action_or_path", help="pull, or a safe ledger JSON path to sync")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bundle-id")
    args = parser.parse_args()
    try:
        store = GitHubLedger(os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", ""), os.getenv("YOUTUBE_LEDGER_BRANCH", "ambience-upload-state"))
        if args.action_or_path == "pull":
            if args.output is None or args.bundle_id is None:
                raise LedgerError("pull requires --output PATH and --bundle-id ID")
            ledger, _ = store.read(args.bundle_id)
            if ledger is None:
                if args.output.exists():
                    raise LedgerError("Remote ledger absent but local state exists; preserve and inspect it before proceeding.")
                print("No previous upload record for this bundle.")
            else:
                if args.output.exists():
                    ledger = merge(ledger, validate(json.loads(args.output.read_text())))
                write_json(args.output, ledger)
                print("Previous nonsecret upload state restored.")
        else:
            path = Path(args.action_or_path)
            ledger = store.push(validate(json.loads(path.read_text())))
            write_json(path, ledger)
            print("Nonsecret upload state saved to the cloud ledger.")
    except (LedgerError, OSError, ValueError) as exc:
        print(f"Ledger error: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
