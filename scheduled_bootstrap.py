"""Move explicitly approved publishing copy into the private draft queue.

The bootstrap specification is read only from the authenticated draft body;
Filebin source links never appear in workflow inputs, public logs or summaries.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import http.cookiejar
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import daily_schedule
import filebin_delivery
from encrypted_queue import pack_private_bundle, restore_private_bundle
from filebin_cloud_state import validate

START = "<!-- quiet-hours-bootstrap:start -->"
END = "<!-- quiet-hours-bootstrap:end -->"
PROBE_NAME = "quiet-shore-approved-connection-copy.txt"


class BootstrapError(Exception):
    pass


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise BootstrapError("Bootstrap download attempted a non-HTTPS redirect.")
        redirected = super().redirect_request(request, response, code, message, headers, url)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


def specification(release: dict, expected_kind: str) -> dict:
    body = release.get("body") or ""
    if body.count(START) != 1 or body.count(END) != 1:
        raise BootstrapError("The private draft needs exactly one complete bootstrap specification.")
    content = body.split(START, 1)[1].split(END, 1)[0].strip()
    try:
        data = json.loads(content)
    except ValueError:
        raise BootstrapError("The private bootstrap specification is not valid JSON.") from None
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("kind") != expected_kind):
        raise BootstrapError("The private bootstrap kind/version does not match this requested operation.")
    return data


def check_source(source: dict, maximum: int) -> None:
    if not isinstance(source, dict) or set(source) != {"url", "sha256", "size"}:
        raise BootstrapError("A bootstrap source needs only its approved URL, byte count and SHA-256.")
    url = urllib.parse.urlsplit(source.get("url", ""))
    if (url.scheme != "https" or url.netloc != "filebin.net" or url.query or url.fragment
            or not re.fullmatch(r"/[a-f0-9]{32}/[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", url.path)
            or type(source.get("size")) is not int or not 0 < source["size"] <= maximum
            or not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])):
        raise BootstrapError("Bootstrap accepts exact approved Filebin files with bounded size and SHA-256.")


def fetch_source(source: dict, destination: Path, maximum: int) -> None:
    check_source(source, maximum)
    if destination.exists():
        raise BootstrapError("Refusing to overwrite an existing bootstrap download.")
    client = urllib.request.build_opener(HTTPSRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        for _ in range(3):
            request = urllib.request.Request(source["url"], headers={"User-Agent": "quiet-hours-private-bootstrap"})
            with client.open(request, timeout=60) as response:
                if response.status != 200:
                    raise BootstrapError("Bootstrap source did not return a complete HTTP 200 response.")
                if response.headers.get_content_type() == "text/html":
                    response.read(1024 * 1024)
                    continue
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".bootstrap-", delete=False) as output:
                    temporary = Path(output.name)
                    count, observed, started = 0, hashlib.sha256(), time.monotonic()
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > source["size"] or time.monotonic() - started > 1200:
                            raise BootstrapError("Bootstrap source exceeded its byte count or transfer deadline.")
                        output.write(chunk)
                        observed.update(chunk)
                if count != source["size"] or observed.hexdigest() != source["sha256"]:
                    raise BootstrapError("Complete bootstrap download failed its size/SHA-256 check.")
                temporary.replace(destination)
                return
        raise BootstrapError("Bootstrap source returned only its first-visit verification page.")
    except (OSError, urllib.error.URLError):
        raise BootstrapError("Approved bootstrap source could not be downloaded; its private URL was not logged.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def upload_private_file(store, source: Path, name: str, sha: str) -> dict:
    """Use the runner's own repository credential, never a transferred token."""
    store.guard()
    size = source.stat().st_size
    with source.open("rb") as stream:
        request = urllib.request.Request(
            f"https://uploads.github.com/repos/{store.repository}/releases/{store.release_id}/assets?"
            + urllib.parse.urlencode({"name": name}), data=stream, method="POST", headers={
                "Authorization": "Bearer " + store.api.token, "Content-Length": str(size),
                "Content-Type": "application/octet-stream", "Accept": "application/vnd.github+json",
                "User-Agent": "quiet-hours-private-bootstrap"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.status != 201:
                    raise BootstrapError("Private bootstrap asset upload was not acknowledged.")
                asset = json.load(response)
        except (OSError, ValueError):
            raise BootstrapError("Private bootstrap asset upload failed; inspect existing private assets before retrying.") from None
    if asset.get("name") != name or asset.get("size") != size or asset.get("state") != "uploaded":
        raise BootstrapError("Private asset upload acknowledgement does not match the approved copy.")
    with tempfile.TemporaryDirectory(prefix="private-bootstrap-check-") as temporary:
        store.download(asset, Path(temporary) / "verified", sha, size)
    return asset


def bootstrap_probe(store, work: Path) -> dict:
    data = specification(store.guard(), "connection-probe")
    if set(data) != {"schema_version", "kind", "source"}:
        raise BootstrapError("Unexpected fields in the approved publishing-copy bootstrap.")
    source = data["source"]
    check_source(source, 128 * 1024)
    if not urllib.parse.urlsplit(source["url"]).path.endswith(".txt"):
        raise BootstrapError("The connection bootstrap accepts only the approved publishing-text file.")
    existing = [asset for asset in store.assets() if asset.get("name") == PROBE_NAME]
    if len(existing) > 1:
        raise BootstrapError("More than one private connection-copy asset exists.")
    if existing:
        asset = existing[0]
        if asset.get("state") != "uploaded" or asset.get("size") != source["size"]:
            raise BootstrapError("An existing private copy differs or is incomplete; it was not overwritten.")
        with tempfile.TemporaryDirectory(prefix="private-probe-check-") as temporary:
            store.download(asset, Path(temporary) / "approved-copy.txt", source["sha256"], source["size"])
        reused = True
    else:
        path = work / "approved-connection-copy.txt"
        fetch_source(source, path, 128 * 1024)
        asset = upload_private_file(store, path, PROBE_NAME, source["sha256"])
        reused = False
    return {"status": "bootstrap_probe_passed", "private_draft_anonymous_http": 404,
            "asset_id": asset["id"], "sha256": source["sha256"], "size": source["size"],
            "reused_existing_asset": reused, "media_delivered": False}


def episode_specification(release: dict) -> tuple[dict, dict, dict, str]:
    data = specification(release, "prepared-bundle")
    if set(data) != {"schema_version", "kind", "bundle_id", "files", "bundle", "motion_review", "delivery_receipt"}:
        raise BootstrapError("The private episode bootstrap contains unexpected fields.")
    receipt = validate(data["delivery_receipt"])
    bundle_id = receipt["bundle_id"]
    if (data["bundle_id"] != bundle_id or receipt.get("status") != "delivered"
            or not isinstance(data["bundle"], dict) or data["bundle"].get("bundle_id") != bundle_id
            or not isinstance(data["files"], list) or len(data["files"]) != 8):
        raise BootstrapError("Bootstrap requires one completed matching eight-file concept delivery.")
    if daily_schedule.aware_time(receipt.get("expires_at", "")) <= datetime.now(timezone.utc):
        raise BootstrapError("The source Filebin package has expired; preserve its existing receipt.")
    sources = {}
    for item in data["files"]:
        if not isinstance(item, dict) or set(item) != {"name", "url", "size", "sha256"}:
            raise BootstrapError("Every episode source needs its exact public filename, URL, size and SHA-256.")
        name = item["name"]
        source = {key: item[key] for key in ("url", "size", "sha256")}
        check_source(source, 2 * 1024**3 - 1)
        if (not isinstance(name, str) or name in sources or name not in receipt["files"]
                or urllib.parse.urlsplit(source["url"]).path.rsplit("/", 1)[-1] != name
                or any(receipt["files"][name].get(key) != source[key] for key in source)):
            raise BootstrapError("Episode source differs from its completed delivery receipt.")
        sources[name] = source
    if set(sources) != set(receipt["expected_files"]) or sum(item["size"] for item in sources.values()) >= 2 * 1024**3 - 1024 * 1024:
        raise BootstrapError("Prepared sources differ from the exact receipt or exceed the private archive limit.")
    artifacts = data["bundle"].get("artifacts")
    keys = (*filebin_delivery.VIDEO_KEYS, *filebin_delivery.IMAGE_KEYS)
    if not isinstance(artifacts, dict) or set(artifacts) != set(keys):
        raise BootstrapError("The embedded bundle must select exactly its six final media artifacts.")
    names = {}
    for key in keys:
        item = artifacts[key]
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BootstrapError("Embedded artifacts need explicit media filenames.")
        suffix = Path(item["path"]).suffix.lower()
        if suffix not in ({".mp4"} if key in filebin_delivery.VIDEO_KEYS else {".jpg", ".jpeg", ".png"}):
            raise BootstrapError("Embedded media has an unsupported extension.")
        names[key] = bundle_id + "-" + key + suffix
    backdrops = [name for name in sources if name in {bundle_id + "-backdrop" + ext for ext in (".jpg", ".jpeg", ".png")}]
    if len(backdrops) != 1 or set(sources) != {*names.values(), backdrops[0], bundle_id + "-upload-details.txt"}:
        raise BootstrapError("Only three videos, thumbnail, two covers, backdrop and publishing copy may be imported.")
    return data, sources, names, backdrops[0]


def check_episode_files(data: dict, paths: dict[str, Path], names: dict, backdrop_name: str, work: Path) -> tuple[Path, Path, Path, str]:
    """Rebase only selected media paths and bind copy/settings to the receipt."""
    bundle = json.loads(json.dumps(data["bundle"]))
    for key, name in names.items():
        bundle["artifacts"][key]["path"] = str(paths[name].resolve())
    videos = bundle.get("videos")
    if (not isinstance(videos, list) or len(videos) != 3
            or any(not isinstance(item, dict) for item in videos)
            or {item.get("key") for item in videos} != set(filebin_delivery.VIDEO_KEYS)):
        raise BootstrapError("Embedded metadata must describe exactly three final videos.")
    for video in videos:
        key = video["key"]
        video["path"] = str(paths[names[key]].resolve())
        if key == "long":
            video["thumbnail"] = str(paths[names["thumbnail"]].resolve())
        else:
            video["cover_frame"] = str(paths[names[key + "-cover"]].resolve())
    bundle_path, motion_path = work / "bundle.json", work / "motion-review.json"
    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n")
    motion_path.write_text(json.dumps(data["motion_review"], indent=2) + "\n")
    backdrop = paths[backdrop_name]
    verified, files, identity = filebin_delivery.build_manifest(bundle_path, backdrop)
    expected = {name: {"size": item["size"], "sha256": item["sha256"]} for name, item in files.items()}
    receipt = data["delivery_receipt"]
    if identity != receipt["identity"] or expected != receipt["expected_files"]:
        raise BootstrapError("Rebased media or generated publishing copy differs from the original completed package.")
    daily_schedule.check_motion_review(motion_path, verified)
    return bundle_path, backdrop, motion_path, identity


def bootstrap_episode(store, work: Path) -> dict:
    data, sources, names, backdrop_name = episode_specification(store.guard())
    bundle_id, receipt = data["bundle_id"], data["delivery_receipt"]
    # Persist existing completed ownership before any prepared-archive upload.
    # A later retry can never mistake this package for a fresh Filebin delivery.
    previous, _ = store.latest_receipt(bundle_id)
    if previous and (previous["identity"] != receipt["identity"] or previous["bin"] != receipt["bin"]):
        raise BootstrapError("A different private delivery receipt already exists for this concept.")
    name = "prepared-" + bundle_id + "-" + receipt["identity"][:16] + ".tar"
    existing = [asset for asset in store.assets() if asset.get("name") == name]
    if len(existing) > 1:
        raise BootstrapError("More than one prepared private archive matches this concept.")
    archive = work / "prepared-bootstrap.tar"
    if existing:
        asset = existing[0]
        if asset.get("state") != "uploaded":
            raise BootstrapError("The existing private archive is incomplete; it was not overwritten.")
        store.download(asset, archive)
        restored = restore_private_bundle(archive, work / "restored-bootstrap", bundle_id=bundle_id)
        _, restored_files, identity = filebin_delivery.build_manifest(restored["bundle_path"], restored["backdrop_path"])
        if (identity != receipt["identity"]
                or json.loads(restored["motion_report_path"].read_text()) != data["motion_review"]):
            raise BootstrapError("The existing prepared archive differs from the selected package or motion review.")
        paths = {name: Path(item["path"]) for name, item in restored_files.items() if "path" in item}
        check_episode_files(data, paths, names, backdrop_name, work)
        packed = {"sha256": filebin_delivery.digest(archive), "size": archive.stat().st_size,
                  "delivery_identity": identity}
    else:
        paths = {}
        for source_name, source in sources.items():
            path = work / "approved-files" / source_name
            fetch_source(source, path, 2 * 1024**3 - 1)
            paths[source_name] = path
        bundle_path, backdrop, motion, identity = check_episode_files(data, paths, names, backdrop_name, work)
        packed = pack_private_bundle(bundle_path, archive, backdrop=backdrop, motion_report=motion)
        if packed["delivery_identity"] != identity:
            raise BootstrapError("Prepared archive identity changed unexpectedly.")
        store.save_receipt(receipt)
        asset = upload_private_file(store, archive, name, packed["sha256"])
    store.save_receipt(receipt)
    page = store.publish_private_review(receipt)
    return {"status": "bootstrap_episode_passed", "bundle_id": bundle_id,
            "private_draft_anonymous_http": 404, "asset_id": asset["id"],
            "sha256": packed["sha256"], "size": packed["size"],
            "delivery_identity": packed["delivery_identity"], "review_page": page,
            "reused_existing_asset": bool(existing), "reused_existing_filebin_delivery": True,
            "media_delivered": False}
