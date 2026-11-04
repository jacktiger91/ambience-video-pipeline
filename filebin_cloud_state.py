#!/usr/bin/env python3
"""Persist only Filebin delivery receipts; never media, copy text or credentials."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys

from cloud_ledger import GitHubLedger, LedgerError, write_json


def validate(data: dict) -> dict:
    required = {"schema_version", "provider", "bin", "bundle_id", "identity", "expected_files", "files"}
    optional = {"expires_at", "readonly", "status", "folder_url"}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        raise LedgerError("Filebin state has unexpected fields; paths, content and credentials are forbidden.")
    if data["schema_version"] != 1 or data["provider"] != "filebin.net":
        raise LedgerError("Unsupported Filebin state format.")
    for field, pattern in (("bin", r"[a-f0-9]{32}"), ("identity", r"[a-f0-9]{64}"),
                           ("bundle_id", r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")):
        if not isinstance(data[field], str) or not re.fullmatch(pattern, data[field]):
            raise LedgerError("Invalid Filebin state identity.")
    expected, files = data["expected_files"], data["files"]
    if not isinstance(expected, dict) or not expected or not isinstance(files, dict) or files.keys() - expected.keys():
        raise LedgerError("Invalid Filebin file manifest.")
    base = "https://filebin.net/" + data["bin"]
    for name, item in expected.items():
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", name)
                or not name.startswith(data["bundle_id"] + "-")
                or not (name.endswith((".mp4", ".jpg", ".jpeg", ".png")) or name.endswith("-upload-details.txt"))
                or not isinstance(item, dict) or set(item) != {"size", "sha256"}
                or type(item["size"]) is not int or item["size"] <= 0
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise LedgerError("Filebin ledger accepts only approved public filenames and size/hash receipts.")
        if name not in files:
            continue
        record = files[name]
        allowed = {"size", "sha256", "url", "uploaded", "download_verified", "download_verification_method"}
        if not isinstance(record, dict) or record.keys() - allowed:
            raise LedgerError("Unexpected file receipt fields.")
        if any(record.get(k) != item[k] for k in ("size", "sha256")) or record.get("url") != base + "/" + name:
            raise LedgerError("File receipt identity differs from its manifest.")
        for flag in ("uploaded", "download_verified"):
            if flag in record and type(record[flag]) is not bool:
                raise LedgerError("Receipt flags must be booleans.")
        if record.get("download_verified") and not record.get("uploaded"):
            raise LedgerError("Download verification requires a confirmed upload.")
        if "download_verification_method" in record and record["download_verification_method"] != "one complete HTTP GET, SHA-256 and byte count":
            raise LedgerError("Unsupported download proof method.")
    if data.get("folder_url", base) != base or ("readonly" in data and type(data["readonly"]) is not bool):
        raise LedgerError("Invalid completed folder receipt.")
    if data.get("expires_at") is not None and (not isinstance(data["expires_at"], str) or not re.fullmatch(r"[0-9T:.+Z-]{10,40}", data["expires_at"])):
        raise LedgerError("Invalid Filebin expiry.")
    if "status" in data and data["status"] != "delivered":
        raise LedgerError("Unsupported delivery status.")
    if data.get("status") == "delivered" and (not data.get("readonly") or set(files) != set(expected) or not all(f.get("download_verified") for f in files.values())):
        raise LedgerError("A delivered receipt needs every download verified and its folder locked.")
    return data


def merge(remote: dict, local: dict) -> dict:
    validate(remote)
    validate(local)
    if any(remote[k] != local[k] for k in ("provider", "bin", "bundle_id", "identity", "expected_files")):
        raise LedgerError("Filebin cloud state conflicts with this run; preserve the existing delivery.")
    result = json.loads(json.dumps(remote))
    for name, item in local["files"].items():
        previous = result["files"].get(name, {})
        result["files"][name] = {**previous, **item}
        for flag in ("uploaded", "download_verified"):
            if flag in previous or flag in item:
                result["files"][name][flag] = bool(previous.get(flag) or item.get(flag))
    for key in ("expires_at", "folder_url"):
        if local.get(key) is not None:
            result[key] = local[key]
    if "readonly" in remote or "readonly" in local:
        result["readonly"] = bool(remote.get("readonly") or local.get("readonly"))
    if remote.get("status") == "delivered" or local.get("status") == "delivered":
        result["status"] = "delivered"
    return validate(result)


class FilebinStore(GitHubLedger):
    def __init__(self, repository: str, token: str):
        super().__init__(repository, token, "ambience-filebin-state")

    def read(self, bundle_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", bundle_id):
            raise LedgerError("Invalid bundle ID.")
        status, data = self.request("GET", f"/contents/filebin-state/{bundle_id}.json?ref={self.branch}")
        if status == 404:
            status, _ = self.request("GET", "")
            if status != 200:
                raise LedgerError("Cannot read the Filebin ledger repository; absence of state is unconfirmed.")
            return None, None
        if status != 200 or data.get("encoding") != "base64":
            raise LedgerError("Cannot read the Filebin cloud ledger.")
        try:
            value = validate(json.loads(base64.b64decode(data["content"])))
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerError("Existing Filebin cloud state is malformed; it was not replaced.") from exc
        if value["bundle_id"] != bundle_id:
            raise LedgerError("Cloud Filebin bundle identity mismatch.")
        return value, data["sha"]

    def push(self, local: dict) -> dict:
        validate(local)
        self.ensure_branch()
        for _ in range(3):
            remote, sha = self.read(local["bundle_id"])
            merged = merge(remote, local) if remote else local
            payload = {"message": "Record Filebin delivery: " + local["bundle_id"], "branch": self.branch,
                       "content": base64.b64encode((json.dumps(merged, indent=2) + "\n").encode()).decode()}
            if sha:
                payload["sha"] = sha
            status, _ = self.request("PUT", "/contents/filebin-state/" + local["bundle_id"] + ".json", payload)
            if status in (200, 201):
                return merged
            if status not in (409, 422):
                break
        raise LedgerError("Filebin cloud state could not be saved; delivery must stop.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pull", "sync"))
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--bundle-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        store = FilebinStore(os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", ""))
        if args.action == "pull":
            if not args.bundle_id or args.output is None:
                raise LedgerError("pull requires --bundle-id and --output.")
            remote, _ = store.read(args.bundle_id)
            if remote is None:
                if args.output.exists():
                    raise LedgerError("Cloud state is absent but local state exists; inspect before uploading.")
                print("No prior Filebin receipt for this bundle.")
            else:
                value = merge(remote, validate(json.loads(args.output.read_text()))) if args.output.exists() else remote
                write_json(args.output, value)
                print("Previous Filebin delivery state restored.")
        else:
            if args.path is None:
                raise LedgerError("sync requires a state path.")
            value = store.push(validate(json.loads(args.path.read_text())))
            write_json(args.path, value)
            print("Filebin delivery state saved.")
    except (LedgerError, OSError, ValueError) as exc:
        print("Filebin state error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
