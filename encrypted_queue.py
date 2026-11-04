#!/usr/bin/env python3
"""Authenticated encrypted prepared bundles and delivery receipts; no network I/O.

The public envelope reveals only its version, purpose, bundle ID and length.
AES-256-GCM authenticates that header and every payload byte. Plaintext is kept
in private temporary files until authentication and archive validation succeed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tarfile
import tempfile

from filebin_delivery import build_manifest, DeliveryError, digest

MAGIC = b"QHENC1\n"
MAX_BYTES = 2 * 1024**3 - 1  # GitHub release assets must remain below 2 GiB.
CHUNK = 1024 * 1024
MEDIA_KEYS = ("long", "short1", "short2", "thumbnail", "short1-cover", "short2-cover")
PROOF_FIELDS = {"full_decode", "resolution", "duration", "video_codec", "audio_codec", "channels",
                "verified_at", "decoded_seconds", "reused_source_full_decode", "source_sha256",
                "normalization", "bytes", "source_offset"}


class QueueError(Exception):
    pass


def load_key(env_name: str = "AMBIENCE_QUEUE_KEY", key_file: Path | None = None) -> bytes:
    """Read one base64-encoded 32-byte key without displaying it."""
    try:
        if key_file is not None:
            if key_file.stat().st_mode & 0o077:
                raise QueueError("The queue key file must be readable only by its owner (mode 600).")
            raw = key_file.read_bytes().strip()
        else:
            raw = os.environ.get(env_name, "").encode().strip()
        key = base64.b64decode(raw, altchars=b"-_", validate=True)
    except (OSError, ValueError):
        raise QueueError("The protected queue key is missing or is not valid base64.") from None
    if len(key) != 32:
        raise QueueError("The protected queue key must decode to exactly 32 bytes.")
    return key


def _identity(key: bytes, purpose: str, bundle_id: str) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise QueueError("AES-256 requires a 32-byte key.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", purpose):
        raise QueueError("Invalid encrypted payload purpose.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", bundle_id):
        raise QueueError("Invalid encrypted bundle identity.")


def _encrypt(source, target, size: int, key: bytes, purpose: str, bundle_id: str) -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _identity(key, purpose, bundle_id)
    nonce = os.urandom(12)
    header = json.dumps({"version": 1, "purpose": purpose, "bundle_id": bundle_id,
                         "nonce": base64.b64encode(nonce).decode(), "plaintext_bytes": size},
                        sort_keys=True, separators=(",", ":")).encode()
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    if size < 0 or len(prefix) + size + 16 > MAX_BYTES:
        raise QueueError("Encrypted payload would exceed the release-asset limit of less than 2 GiB.")
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    target.write(prefix)
    copied = 0
    while block := source.read(CHUNK):
        copied += len(block)
        if copied > size:
            raise QueueError("Plaintext changed during encryption.")
        target.write(encryptor.update(block))
    if copied != size:
        raise QueueError("Plaintext changed during encryption.")
    target.write(encryptor.finalize())
    target.write(encryptor.tag)


def _decrypt(source, target, key: bytes, purpose: str, bundle_id: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _identity(key, purpose, bundle_id)
    source.seek(0, os.SEEK_END)
    total = source.tell()
    source.seek(0)
    if total > MAX_BYTES or source.read(len(MAGIC)) != MAGIC:
        raise QueueError("Invalid or oversized encrypted envelope.")
    length_bytes = source.read(4)
    if len(length_bytes) != 4:
        raise QueueError("Truncated encrypted envelope.")
    length = struct.unpack(">I", length_bytes)[0]
    if not 1 <= length <= 4096:
        raise QueueError("Invalid encrypted header length.")
    header_bytes = source.read(length)
    try:
        header = json.loads(header_bytes)
        if (not isinstance(header, dict) or set(header) != {"version", "purpose", "bundle_id", "nonce", "plaintext_bytes"}
                or header["version"] != 1 or header["purpose"] != purpose or header["bundle_id"] != bundle_id
                or type(header["plaintext_bytes"]) is not int or header["plaintext_bytes"] < 0):
            raise ValueError
        nonce = base64.b64decode(header["nonce"], validate=True)
        if len(nonce) != 12:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise QueueError("Encrypted payload identity or header is invalid.") from None
    offset = len(MAGIC) + 4 + length
    size = header["plaintext_bytes"]
    if offset + size + 16 != total:
        raise QueueError("Encrypted payload is truncated or has trailing bytes.")
    source.seek(-16, os.SEEK_END)
    tag = source.read(16)
    source.seek(offset)
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(MAGIC + length_bytes + header_bytes)
    remaining = size
    try:
        while remaining:
            block = source.read(min(CHUNK, remaining))
            if not block:
                raise QueueError("Encrypted payload is truncated.")
            target.write(decryptor.update(block))
            remaining -= len(block)
        target.write(decryptor.finalize())
    except InvalidTag:
        raise QueueError("Encrypted payload authentication failed; no plaintext was released.") from None


def encrypt_bytes(data: bytes, key: bytes, *, purpose: str, bundle_id: str) -> bytes:
    output = io.BytesIO()
    _encrypt(io.BytesIO(data), output, len(data), key, purpose, bundle_id)
    return output.getvalue()


def decrypt_bytes(data: bytes, key: bytes, *, purpose: str, bundle_id: str) -> bytes:
    output = io.BytesIO()
    _decrypt(io.BytesIO(data), output, key, purpose, bundle_id)
    return output.getvalue()  # Reached only after GCM authentication succeeds.


def _crypt_file(source: Path, output: Path, key: bytes, purpose: str, bundle_id: str, decrypt: bool) -> dict:
    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_file() or output.exists() or output.is_symlink():
        raise QueueError("Input must exist and output must be a new file.")
    output.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    with tempfile.TemporaryDirectory(prefix=".queue-crypto-", dir=output.parent) as temporary:
        candidate = Path(temporary) / "payload"
        with source.open("rb") as src, candidate.open("xb") as dest:
            os.chmod(candidate, 0o600)
            if decrypt:
                _decrypt(src, dest, key, purpose, bundle_id)
            else:
                _encrypt(src, dest, before.st_size, key, purpose, bundle_id)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise QueueError("Input changed during encryption/decryption.")
        result = {"sha256": digest(candidate), "size": candidate.stat().st_size,
                  "purpose": purpose, "bundle_id": bundle_id}
        os.link(candidate, output)  # Atomic, and never overwrites an existing destination.
    return result


def encrypt_file(source: Path, output: Path, key: bytes, *, purpose: str, bundle_id: str) -> dict:
    return _crypt_file(source, output, key, purpose, bundle_id, False)


def decrypt_file(source: Path, output: Path, key: bytes, *, purpose: str, bundle_id: str) -> dict:
    return _crypt_file(source, output, key, purpose, bundle_id, True)


def _motion(path: Path, bundle: dict) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise QueueError("A final-export motion review is required for the prepared queue.") from None
    if (not isinstance(data, dict) or set(data) != {"bundle_id", "videos"}
            or data["bundle_id"] != bundle["bundle_id"] or not isinstance(data["videos"], dict)
            or set(data["videos"]) != {"long", "short1", "short2"}):
        raise QueueError("Motion review must identify this bundle and all three final exports.")
    for video in bundle["videos"]:
        item = data["videos"][video["key"]]
        if (not isinstance(item, dict) or set(item) != {"sha256", "subject_motion_visible", "camera_fixed", "matches_concept", "review_notes"}
                or item["sha256"] != video["sha256"]
                or any(item[flag] is not True for flag in ("subject_motion_visible", "camera_fixed", "matches_concept"))
                or not isinstance(item["review_notes"], str) or not item["review_notes"].strip()
                or len(item["review_notes"]) > 4000):
            raise QueueError("Each final export needs matching hashes and actual motion-review observations.")
    return data


def _portable(bundle: dict) -> dict:
    fields = {"schema_version", "bundle_id", "source_sha256", "intended_channel_id", "intended_channel_handle",
              "theme", "playlist_id", "recording_credits", "requested_privacy", "schedule_enabled", "ready", "studio_manual_steps"}
    result = {key: bundle[key] for key in fields if key in bundle}
    result["artifacts"] = {}
    for key in MEDIA_KEYS:
        old = bundle["artifacts"][key]
        result["artifacts"][key] = {"path": key + Path(old["path"]).suffix.lower(), "sha256": old["sha256"],
            "verification": {k: v for k, v in old["verification"].items() if k in PROOF_FIELDS}}
    video_fields = {"key", "sha256", "title", "description", "thumbnail_sha256", "recording_credits", "cover_frame_sha256",
                    "suggested_cover_offset", "source_start", "framing", "crop_position", "cta", "made_for_kids", "contains_synthetic_media"}
    result["videos"] = []
    for old in bundle["videos"]:
        item = {key: old[key] for key in video_fields if key in old}
        item["path"] = result["artifacts"][item["key"]]["path"]
        item["verification"] = {k: v for k, v in old["verification"].items() if k in PROOF_FIELDS}
        if item["key"] == "long":
            item["thumbnail"] = result["artifacts"]["thumbnail"]["path"]
        else:
            item["cover_frame"] = result["artifacts"][item["key"] + "-cover"]["path"]
        result["videos"].append(item)
    return result


def pack_private_bundle(bundle_path: Path, output: Path, *, backdrop: Path, motion_report: Path) -> dict:
    """Pack for an access-controlled draft release, never a public release.

This offline helper cannot establish remote privacy. The transport must verify
that the destination is still a private draft before sending the archive.
"""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise QueueError("Archive output must be a new file.")
    bundle, files, identity = build_manifest(Path(bundle_path), Path(backdrop))
    portable = _portable(bundle)
    motion = _motion(Path(motion_report), portable)
    media = {portable["artifacts"][key]["path"]: Path(bundle["artifacts"][key]["path"]) for key in MEDIA_KEYS}
    for name, source in list(media.items()):
        if not source.is_absolute():
            media[name] = Path(bundle_path).resolve().parent / source
    backdrop_name = "backdrop" + Path(backdrop).suffix.lower()
    media[backdrop_name] = Path(backdrop).resolve()
    documents = {"bundle.json": json.dumps(portable, sort_keys=True).encode(),
                 "motion-review.json": json.dumps(motion, sort_keys=True).encode()}
    records = {name: {"sha256": digest(path), "size": path.stat().st_size} for name, path in media.items()}
    expected_sources = {Path(item["path"]).resolve(): item for item in files.values() if "path" in item}
    if any(records[name]["sha256"] != expected_sources[path.resolve()]["sha256"]
           or records[name]["size"] != expected_sources[path.resolve()]["size"] for name, path in media.items()):
        raise QueueError("A selected media file changed after bundle verification.")
    records.update({name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)} for name, value in documents.items()})
    manifest = {"schema_version": 1, "bundle_id": bundle["bundle_id"], "delivery_identity": identity,
                "backdrop": backdrop_name, "files": records}
    documents["queue-manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".queue-pack-", dir=Path(output).parent) as temporary:
        archive = Path(temporary) / "bundle.tar"
        with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as tar:
            os.chmod(archive, 0o600)
            for name, source in media.items():
                info = tarfile.TarInfo(name); info.size = records[name]["size"]; info.mode = 0o600
                with source.open("rb") as stream:
                    tar.addfile(info, stream)
                if digest(source) != records[name]["sha256"]:
                    raise QueueError("A selected media file changed while packing.")
            for name, value in documents.items():
                info = tarfile.TarInfo(name); info.size = len(value); info.mode = 0o600
                tar.addfile(info, io.BytesIO(value))
        if archive.stat().st_size > MAX_BYTES:
            raise QueueError("Prepared archive exceeds the release-asset limit of less than 2 GiB.")
        result = {"sha256": digest(archive), "size": archive.stat().st_size,
                  "purpose": "prepared-bundle", "bundle_id": bundle["bundle_id"]}
        os.link(archive, output)
    return {**result, "delivery_identity": identity}


def pack_bundle(bundle_path: Path, output: Path, key: bytes, *, backdrop: Path, motion_report: Path) -> dict:
    """Optional encrypted transport when private draft-release storage is unavailable."""
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".queue-seal-", dir=Path(output).parent) as temporary:
        archive = Path(temporary) / "bundle.tar"
        prepared = pack_private_bundle(bundle_path, archive, backdrop=backdrop, motion_report=motion_report)
        result = encrypt_file(archive, Path(output), key, purpose="prepared-bundle", bundle_id=prepared["bundle_id"])
    return {**result, "delivery_identity": prepared["delivery_identity"]}


def _restore_bundle(source: Path, output_directory: Path, key: bytes | None, *, bundle_id: str) -> dict:
    output = Path(output_directory).absolute()
    if output.exists() or output.is_symlink():
        raise QueueError("Restore destination must be a new directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".queue-restore-", dir=output.parent) as temporary:
        root = Path(temporary); archive = root / "bundle.tar"; stage = root / "ready"
        if key is None:
            source = Path(source)
            before = source.stat()
            if not source.is_file() or before.st_size > MAX_BYTES:
                raise QueueError("Invalid or oversized prepared archive.")
            shutil.copyfile(source, archive)
            os.chmod(archive, 0o600)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise QueueError("Prepared archive changed during restoration.")
        else:
            decrypt_file(Path(source), archive, key, purpose="prepared-bundle", bundle_id=bundle_id)
        stage.mkdir(mode=0o700)
        with tarfile.open(archive, "r:") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if (len(names) != len(set(names)) or len(names) != 10 or sum(m.size for m in members) > MAX_BYTES
                    or any(not m.isfile() or not re.fullmatch(r"[a-z0-9-]+\.(?:json|mp4|jpg|jpeg|png)", m.name) or m.size < 0 for m in members)):
                raise QueueError("Archive contains unexpected paths, links, files or sizes.")
            for member in members:
                if member.name.endswith(".json") and member.size > 1024 * 1024:
                    raise QueueError("Archive metadata is oversized.")
                with tar.extractfile(member) as src, (stage / member.name).open("xb") as dest:
                    os.chmod(stage / member.name, 0o600)
                    shutil.copyfileobj(src, dest, CHUNK)
        try:
            manifest = json.loads((stage / "queue-manifest.json").read_text())
            if (set(manifest) != {"schema_version", "bundle_id", "delivery_identity", "backdrop", "files"}
                    or manifest["schema_version"] != 1 or manifest["bundle_id"] != bundle_id
                    or not re.fullmatch(r"backdrop\.(?:jpg|jpeg|png)", manifest["backdrop"])
                    or set(manifest["files"]) != set(names) - {"queue-manifest.json"}):
                raise ValueError
            for name, record in manifest["files"].items():
                path = stage / name
                if set(record) != {"size", "sha256"} or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
                    raise ValueError
            portable = json.loads((stage / "bundle.json").read_text())
            if portable != _portable(portable):
                raise ValueError  # Reject absolute/external paths and extra private fields.
            bundle, files, identity = build_manifest(stage / "bundle.json", stage / manifest["backdrop"])
            if bundle["bundle_id"] != bundle_id or identity != manifest["delivery_identity"]:
                raise ValueError
            selected = {Path(item["path"]).name for item in files.values() if "path" in item}
            if selected | {"bundle.json", "motion-review.json", "queue-manifest.json"} != set(names):
                raise ValueError
            _motion(stage / "motion-review.json", bundle)
        except (OSError, ValueError, TypeError, KeyError, DeliveryError):
            raise QueueError("Authenticated archive failed its allowlist, hash or bundle checks.") from None
        output.mkdir(mode=0o700)  # Claim a new name without replacing user files.
        try:
            os.replace(stage, output)
        except BaseException:
            output.rmdir()
            raise
    return {"bundle_path": output / "bundle.json", "backdrop_path": output / manifest["backdrop"],
            "motion_report_path": output / "motion-review.json", "delivery_identity": identity}


def restore_private_bundle(source: Path, output_directory: Path, *, bundle_id: str) -> dict:
    """Restore a hash-verified archive downloaded from a private draft release."""
    return _restore_bundle(source, output_directory, None, bundle_id=bundle_id)


def restore_bundle(source: Path, output_directory: Path, key: bytes, *, bundle_id: str) -> dict:
    return _restore_bundle(source, output_directory, key, bundle_id=bundle_id)


pack_plain_bundle = pack_private_bundle
restore_plain_bundle = restore_private_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-env", default="AMBIENCE_QUEUE_KEY")
    parser.add_argument("--key-file", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("pack", "pack-private"):
        pack = sub.add_parser(action)
        pack.add_argument("--bundle", type=Path, required=True)
        pack.add_argument("--backdrop", type=Path, required=True)
        pack.add_argument("--motion-report", type=Path, required=True)
        pack.add_argument("--output", type=Path, required=True)
    for action in ("restore", "restore-private", "encrypt", "decrypt"):
        command = sub.add_parser(action)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--bundle-id", required=True)
        if action not in {"restore", "restore-private"}: command.add_argument("--purpose", required=True)
    args = parser.parse_args()
    try:
        key = load_key(args.key_env, args.key_file) if not args.action.endswith("-private") else None
        if args.action == "pack-private":
            result = pack_private_bundle(args.bundle, args.output, backdrop=args.backdrop, motion_report=args.motion_report)
        elif args.action == "restore-private":
            result = restore_private_bundle(args.input, args.output, bundle_id=args.bundle_id)
        elif args.action == "pack":
            result = pack_bundle(args.bundle, args.output, key, backdrop=args.backdrop, motion_report=args.motion_report)
        elif args.action == "restore":
            result = restore_bundle(args.input, args.output, key, bundle_id=args.bundle_id)
        else:
            result = (encrypt_file if args.action == "encrypt" else decrypt_file)(args.input, args.output, key, purpose=args.purpose, bundle_id=args.bundle_id)
        print(json.dumps(result, default=str, indent=2))
    except (QueueError, DeliveryError, OSError, tarfile.TarError) as exc:
        print("Queue error: " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
