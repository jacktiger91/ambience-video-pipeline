#!/usr/bin/env python3
"""Select today's prepared concept and check its delivery deadline without uploads.

This is a read-only preflight, not a scheduler. A deployed runner must restore
private assets and retry receipts before calling it, then call filebin_delivery
only when ``eligible_for_attempt`` is true. It never rolls back to an old date.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import filebin_delivery
from filebin_cloud_state import validate as validate_receipt
from cloud_ledger import LedgerError

ROOT = Path(__file__).resolve().parent


class ScheduleError(Exception):
    pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ScheduleError("A required scheduling document is missing or invalid.") from None
    if not isinstance(value, dict):
        raise ScheduleError("Scheduling documents must contain JSON objects.")
    return value


def local_path(config_path: Path, value) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError("Prepared packages need explicit local paths after restoration.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else config_path.parent / path).resolve()


def wall_time(value) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ScheduleError("Delivery times must use HH:MM in the configured timezone.")
    return time.fromisoformat(value)


def aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ScheduleError("Use an ISO timestamp with its UTC offset.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduleError("Timestamp timezone is required; host-local time is not assumed.")
    return parsed


def check_motion_review(path: Path, bundle: dict) -> None:
    report = read_json(path)
    if report.get("bundle_id") != bundle["bundle_id"] or not isinstance(report.get("videos"), dict):
        raise ScheduleError("Motion review does not identify this exact finished bundle.")
    for video in bundle["videos"]:
        proof = report["videos"].get(video["key"])
        if (not isinstance(proof, dict) or proof.get("sha256") != video["sha256"]
                or any(proof.get(flag) is not True for flag in
                       ("subject_motion_visible", "camera_fixed", "matches_concept"))
                or not isinstance(proof.get("review_notes"), str) or not proof["review_notes"].strip()):
            raise ScheduleError("All three final exports need hash-bound playback observations of visible scene motion.")


def select_slot(config_path: Path, now: datetime | None = None, episode_date: date | None = None) -> dict:
    config_path = config_path.expanduser().resolve()
    config = read_json(config_path)
    if config.get("schema_version") != 1 or type(config.get("enabled")) is not bool:
        raise ScheduleError("Unsupported schedule configuration.")
    try:
        zone = ZoneInfo(config["timezone"])
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        raise ScheduleError("A valid IANA delivery timezone is required.") from None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleError("The current timestamp must be timezone-aware.")
    local_now = now.astimezone(zone)
    day = episode_date or local_now.date()
    opens = datetime.combine(day, wall_time(config.get("attempt_from")), zone)
    deadline = datetime.combine(day, wall_time(config.get("ready_by")), zone)
    if opens >= deadline:
        raise ScheduleError("Start delivery before the same-day ready-by deadline.")
    catalog = read_json(local_path(config_path, config.get("concept_catalog")))
    try:
        first = date.fromisoformat(catalog["rotation_start_date"])
        concepts = catalog["concepts"]
    except (KeyError, TypeError, ValueError):
        raise ScheduleError("The dated concept catalog is invalid.") from None
    if not isinstance(concepts, list) or not concepts:
        raise ScheduleError("The dated catalog has no concepts.")
    prepared = config.get("prepared_episodes")
    if not isinstance(prepared, dict):
        raise ScheduleError("prepared_episodes must map exact dates to prepared package paths.")
    blockers = []
    result = {
        "schema_version": 1, "timezone": str(zone), "episode_date": day.isoformat(),
        "checked_at": local_now.isoformat(), "attempt_from": opens.isoformat(),
        "ready_by": deadline.isoformat(), "ready_by_utc": deadline.astimezone(timezone.utc).isoformat(),
        "enabled": config["enabled"], "window_open": local_now >= opens,
        "deadline_passed": local_now > deadline, "local_package_ready": False,
        "receipt_status": "absent", "eligible_for_attempt": False,
        "blockers": blockers, "network_used": False,
    }
    if not config["enabled"]:
        blockers.append("Scheduled execution is disabled; this check does not deploy a runner.")
    if config.get("first_delivery_date") is not None:
        try:
            first_delivery = date.fromisoformat(config["first_delivery_date"])
        except (TypeError, ValueError):
            raise ScheduleError("first_delivery_date must use YYYY-MM-DD.") from None
        if day < first_delivery:
            blockers.append("The first requested daily delivery is on " + first_delivery.isoformat() + ".")
    production_start = config.get("automatic_production_start")
    if production_start is not None:
        try:
            starts = date.fromisoformat(production_start)
        except (TypeError, ValueError):
            raise ScheduleError("automatic_production_start must use YYYY-MM-DD.") from None
        if day >= starts:
            # A stable dated identity permits a fresh researched concept even
            # after the original inspiration catalogue ends. Content must come
            # from that date's verified private production entry, never an old
            # episode or merely a recycled title.
            result.update({"concept_id": "daily-ambience", "bundle_id": day.isoformat() + "-daily-ambience"})
            return result
    offset = (day - first).days
    if not 0 <= offset < len(concepts):
        blockers.append("No concept exists for this local date. Extend the queue; do not recycle an earlier episode.")
        return result
    concept = concepts[offset]
    if not isinstance(concept, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", concept.get("id", "")):
        raise ScheduleError("Invalid dated concept identity.")
    bundle_id = day.isoformat() + "-" + concept["id"]
    result.update({"concept_id": concept["id"], "bundle_id": bundle_id})
    return result


def assess(config_path: Path, now: datetime | None = None) -> dict:
    result = select_slot(config_path, now)
    if "bundle_id" not in result:
        return result
    config_path = config_path.expanduser().resolve()
    config = read_json(config_path)
    day = date.fromisoformat(result["episode_date"])
    now = aware_time(result["checked_at"])
    deadline = aware_time(result["ready_by"])
    bundle_id = result["bundle_id"]
    blockers = result["blockers"]
    prepared = config["prepared_episodes"]
    entry = prepared.get(day.isoformat())
    if not isinstance(entry, dict):
        blockers.append("Today's concept has no restored, verified media package yet.")
        return result
    if entry.get("bundle_id") != bundle_id:
        raise ScheduleError("Prepared package belongs to a different day or concept; it cannot fill today's slot.")
    # Optional deadline describes the source/restoration lease, not Filebin's
    # eventual review-link expiry. A seven-day handoff is not a permanent queue.
    if entry.get("source_available_until") is not None:
        expires = aware_time(entry["source_available_until"])
        if expires <= max(now, deadline):
            blockers.append("Prepared asset source expires before this delivery window completes.")
            return result
    try:
        bundle_path = local_path(config_path, entry.get("bundle"))
        backdrop_path = local_path(config_path, entry.get("backdrop"))
        bundle, files, identity = filebin_delivery.build_manifest(bundle_path, backdrop_path)
        if bundle["bundle_id"] != bundle_id:
            raise ScheduleError("Verified bundle identity differs from today's concept.")
        check_motion_review(local_path(config_path, entry.get("motion_review")), bundle)
        receipt_path = local_path(config_path, entry.get("receipt"))
        if receipt_path.exists():
            receipt = validate_receipt(read_json(receipt_path))
            expected = {name: {"size": item["size"], "sha256": item["sha256"]} for name, item in files.items()}
            if (receipt["bundle_id"] != bundle_id or receipt["identity"] != identity
                    or receipt["expected_files"] != expected):
                raise ScheduleError("Retained receipt belongs to different bytes; preserve it before resolving the mismatch.")
            result["receipt_status"] = "delivered" if receipt.get("status") == "delivered" else "partial"
    except (ScheduleError, filebin_delivery.DeliveryError, LedgerError, OSError) as exc:
        blockers.append(str(exc))
        return result
    result["local_package_ready"] = True
    result["file_count"] = len(files)
    # A completed receipt remains completed even if its temporary links later
    # expire. Refreshing an archive is a separate deliberate action, not retry.
    result["eligible_for_attempt"] = bool(config["enabled"] and result["window_open"]
                                          and result["receipt_status"] != "delivered" and not blockers)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/scheduled-delivery.json")
    parser.add_argument("--now", help="ISO timestamp with UTC offset; omitted uses current UTC clock")
    parser.add_argument("--require-ready", action="store_true", help="exit 2 unless today's local package is verified and ready")
    args = parser.parse_args()
    try:
        result = assess(args.config, aware_time(args.now) if args.now else None)
        print(json.dumps(result, indent=2))
        return 2 if args.require_ready and not result["local_package_ready"] else 0
    except ScheduleError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
