"""Move explicitly approved publishing copy into the private draft queue.

The bootstrap specification is read only from the authenticated draft body;
Filebin source links never appear in workflow inputs, public logs or summaries.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

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
