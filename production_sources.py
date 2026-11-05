"""Free, attributed field-recording and camera-footage discovery.

Remote descriptions are data, never commands. Only explicit CC0/CC BY grants
are accepted. Downloads are bounded and checked against the source catalogue.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
from pathlib import Path
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

AGENT = "QuietHoursAmbience/1.0 (licensed nature media; github.com/jacktiger91/ambience-video-pipeline)"
FAMILIES = {
    "coast": {"label": "Gentle Waves", "search": '(title:waves OR title:sea OR title:shore OR title:beach)', "visual_search": 'waves shore filetype:video', "themes": ["Quiet Bay", "Gentle Shore", "Tidal Calm", "Coastal Pause"]},
    "meadow": {"label": "Meadow Sounds", "search": '(title:crickets OR title:meadow OR title:grassland)', "visual_search": 'meadow grass wind filetype:video', "themes": ["Meadow Breeze", "Quiet Meadow", "Grassland Calm", "Open Meadow"]},
    "stream": {"label": "Flowing Water", "search": '(title:stream OR title:brook OR title:creek OR title:river)', "visual_search": 'stream creek flowing water filetype:video', "themes": ["Hidden Stream", "River Calm", "Woodland Brook", "Flowing Peace"]},
    "rain": {"label": "Natural Rain", "search": '(title:rain OR title:rainfall)', "visual_search": 'rain falling filetype:video', "themes": ["Soft Rain", "Rainfall Retreat", "Gentle Rain", "Rainy Stillness"]},
    "birds": {"label": "Gentle Birdsong", "search": '(title:birdsong OR title:"dawn chorus" OR title:birds OR title:forest)', "visual_search": 'forest leaves wind filetype:video', "themes": ["Woodland Birds", "Morning Canopy", "Forest Calm", "Leafy Retreat"]},
    "water": {"label": "Water and Reeds", "search": '(title:lake OR title:water OR title:wetland OR title:pond)', "visual_search": 'lake ripples reeds filetype:video', "themes": ["Lake Ripples", "Quiet Water", "Reed Haven", "Waterside Calm"]},
}
# Adjacent days have different sound families. A dated episode uses fresh
# source identities; this is not permission to recycle finished videos.
ROTATION = ["coast", "meadow", "stream", "rain", "birds", "water", "coast", "birds", "stream", "rain", "water", "meadow"]
SEED_VISUALS = {
    "coast": [("ThroughTheReeds", "Quiet Bay"), ("EdgeOfWater", "Water's Edge"), ("SunsetWavesWideShot", "Sunset Waves"), ("BeachroyaltyFreeStockVideo", "Open Shore"), ("LogsInTheWater", "Sheltered Bay")],
    "meadow": [("MeadowAndHillsCCBYNatureclip", "Meadow Breeze"), ("CattailLeaves", "Rustling Reeds")],
    "stream": [("RelaxAtTheRiverLakeCCBYNatureClip", "River Haven")],
    "rain": [],
    "birds": [("ForestBackground", "Forest Breeze"), ("LightAmongstTheLeavesCCBYNatureClip", "Leafy Canopy"), ("SilentForestCCBYNatureClip", "Woodland Calm"), ("Stock_Footage_Of_A_Backlit_Leaf", "Autumn Light")],
    "water": [("lakeshoreline_close_up", "Lake Ripples"), ("Reeds", "Reed Haven"), ("SwanLakeCCBYNatureClip", "Swan Lake")],
}
EXCLUDED_VISUAL = re.compile(r"time.?lapse|aerial|drone|bird.?s.eye|panning|dolly|tilt|animation|cgi\b|synthetic|computer.generated|simulation|slideshow|slow.motion", re.I)
EXCLUDED_AUDIO = re.compile(r"synthesi[sz]|sound.art|sound installation|musical performance|improvisation|sound collage|electronic composition", re.I)
DISTRACTIONS = re.compile(r"\b(?:teenagers|children|crowds?|shouting|chatting|conversation|talking|motorcycles?|motorbikes?|race the waves|cafe|café|restaurant|music|traffic|construction|jackhammer|powerboats?|car engines?|highway|contact mics|contact microphones|people walking|human noises)\b", re.I)


class SourceError(Exception):
    pass


def plain(value) -> str:
    if isinstance(value, list):
        value = "; ".join(str(x) for x in value)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()


def licence(value) -> tuple[str, str]:
    """Exact grants only: never infer permission from 'free' or a search hit."""
    values = value if isinstance(value, list) else [value]
    grants = []
    for raw in values:
        u = urllib.parse.urlsplit(str(raw).strip())
        if u.scheme not in {"http", "https"} or u.hostname not in {"creativecommons.org", "www.creativecommons.org"}:
            raise SourceError("Source does not declare an accepted commercial-use licence.")
        path = u.path.rstrip("/")
        if path == "/publicdomain/zero/1.0":
            grants.append(("CC0-1.0", "https://creativecommons.org/publicdomain/zero/1.0/"))
        elif re.fullmatch(r"/licenses/by/(?:2\.0|2\.5|3\.0|4\.0)(?:/au)?", path):
            grants.append(("CC-BY-" + path.split("/")[3] + ("-AU" if path.endswith("/au") else ""), "https://creativecommons.org" + path + "/"))
        else:
            raise SourceError("Noncommercial, no-derivatives, share-alike and unspecified grants are not accepted by this workflow.")
    if len(set(grants)) != 1:
        raise SourceError("Conflicting source licences require review.")
    return grants[0]


def get_json(url: str) -> dict:
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise SourceError("Source metadata exceeds its bound.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (OSError, ValueError):
            if attempt == 2:
                raise SourceError("Public source metadata is unavailable or invalid.") from None
            time.sleep(2 ** attempt)
    raise SourceError("Metadata could not be read.")


def metadata(identifier: str, cache: Path) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", identifier):
        raise SourceError("Invalid archive identifier.")
    path = cache / (identifier + ".json")
    if path.exists():
        return json.loads(path.read_text())
    value = get_json("https://archive.org/metadata/" + identifier)
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return value


def archive_record(identifier: str, kind: str, cache: Path) -> dict:
    data = metadata(identifier, cache)
    m = data.get("metadata", {})
    grant, link = licence(m.get("licenseurl"))
    author, title, description = (plain(m.get(x)) for x in ("creator", "title", "description"))
    if not author or not title:
        raise SourceError("Source needs an identified creator and title.")
    collection = m.get("collection", [])
    collection = [collection] if isinstance(collection, str) else collection
    context = re.sub(r"\b(?:no|without|free of)\s+(?:any\s+)?(?:traffic|music|voices|people|wind)\b", "", title + " " + description, flags=re.I)
    if kind == "audio" and ("radio-aporee-maps" not in collection or EXCLUDED_AUDIO.search(context) or DISTRACTIONS.search(context)):
        raise SourceError("Audio must be a documented real field recording from the source collection.")
    if kind == "video" and (not ("Jeffrey Beach" in author or author == "NatureClip") or EXCLUDED_VISUAL.search(title + " " + description)):
        raise SourceError("This stock-video candidate is not suitable real fixed-camera footage.")
    choices = []
    for item in data.get("files", []):
        name = item.get("name", "")
        suffix = Path(name).suffix.lower()
        try:
            size, duration = int(item.get("size", 0)), float(item.get("length", 0))
            width, height = int(item.get("width", 0)), int(item.get("height", 0))
        except (ValueError, TypeError):
            continue
        if kind == "audio":
            eligible = suffix in {".mp3", ".flac", ".wav"} and item.get("source") == "original" and 60 <= duration <= 18000 and 1000 < size < 500 * 1024**2
        else:
            eligible = suffix in {".mp4", ".mov", ".webm"} and width >= 1920 and height >= 1080 and 18 <= duration <= 1800 and 1000 < size < 3 * 1024**3
        if eligible and re.fullmatch(r"[a-f0-9]{32}", item.get("md5", "")):
            choices.append((item.get("source") != "original", suffix != ".mp4" if kind == "video" else suffix != ".mp3", size, item))
    if not choices:
        raise SourceError("No suitable complete original stereo-audio candidate or HD camera clip is available.")
    item = min(choices, key=lambda x: x[:3])[3]
    name = item["name"]
    return {"id": "archive:" + identifier, "provider": "archive", "identifier": identifier,
            "kind": kind, "author": author, "title": title, "description": description,
            "license": grant, "license_url": link, "source_page": "https://archive.org/details/" + identifier,
            "url": "https://archive.org/download/" + identifier + "/" + urllib.parse.quote(name, safe="/"),
            "filename": name, "bytes": int(item["size"]), "md5": item["md5"],
            "duration_hint": float(item["length"]), "metadata_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            "field_recording_verified_from_creator_description": kind == "audio"}


def audio_candidates(family: str, cache: Path, used: set[str], seed: int) -> list[dict]:
    allowed = ' OR '.join('licenseurl:"' + scheme + '://creativecommons.org/' + path + '"' for scheme in ("http", "https") for path in ("licenses/by/3.0/", "licenses/by/4.0/", "publicdomain/zero/1.0/"))
    query = "collection:radio-aporee-maps AND " + FAMILIES[family]["search"] + " AND (" + allowed + ")"
    ids=[]
    for page in range(1,11):
        url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode({"q": query, "output": "json", "rows": 150, "page":page, "fl[]": "identifier", "sort[]": "date desc"})
        docs = get_json(url).get("response", {}).get("docs", [])
        ids.extend(x["identifier"] for x in docs if "archive:" + x.get("identifier", "") not in used)
        if len(ids)>=120 or len(docs)<150:break
    if family == "coast":
        ids = list(dict.fromkeys([*ids, *[x for x in ("aporee_59222_67965", "aporee_71019_82826", "aporee_63075_72558") if "archive:"+x not in used]]))
    random.Random(seed).shuffle(ids)
    def candidate(identifier):
        try:
            return archive_record(identifier, "audio", cache)
        except SourceError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        records = [x for x in pool.map(candidate, ids[:160]) if x]
    # Long continuous originals reduce transitions and the number of downloads.
    records.sort(key=lambda x: -x["duration_hint"])
    return records[:32]


def commons_candidates(family: str, used: set[str], offset: int = 0) -> list[dict]:
    query = {"action": "query", "format": "json", "generator": "search", "gsrsearch": FAMILIES[family]["visual_search"],
             "gsrnamespace": 6, "gsrlimit": 50, "gsroffset":offset, "prop": "imageinfo", "iiprop": "url|size|sha1|extmetadata",
             "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|ImageDescription|Credit"}
    data = get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(query))
    records = []
    for page in data.get("query", {}).get("pages", {}).values():
        try:
            info = page["imageinfo"][0]
            extra = info["extmetadata"]
            grant, link = licence(extra["LicenseUrl"]["value"])
            title = plain(page["title"])[5:]
            author = plain(extra["Artist"]["value"])
            description = plain(extra.get("ImageDescription", {}).get("value", ""))
            identity = "commons:" + str(page["pageid"])
            if (identity in used or not author or EXCLUDED_VISUAL.search(title + " " + description)
                    or info["width"] < 1920 or info["height"] < 1080 or not 10000 < info["size"] < 1024**3
                    or Path(urllib.parse.urlsplit(info["url"]).path).suffix.lower() not in {".webm", ".ogv", ".mp4"}):
                continue
            sha = info.get("sha1", "")
            sha = f"{int(sha, 36):040x}" if len(sha) != 40 else sha
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                continue
            records.append({"id": identity, "provider": "commons", "kind": "video", "author": author,
                            "title": title, "description": description, "license": grant, "license_url": link,
                            "source_page": info["descriptionurl"], "url": info["url"], "filename": title,
                            "bytes": info["size"], "sha1": sha,
                            "metadata_sha256": hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()})
        except (KeyError, ValueError, TypeError, SourceError):
            continue
    return records


def visual_candidates(family: str, cache: Path, used: set[str], seed: int) -> list[dict]:
    result = []
    for identifier, theme in SEED_VISUALS[family]:
        if "archive:" + identifier in used:
            continue
        try:
            result.append({**archive_record(identifier, "video", cache), "theme_title": theme})
        except SourceError:
            continue
    extra = commons_candidates(family, used)
    for offset in (50,100,150):
        if len(extra)>=15:break
        extra.extend(commons_candidates(family,used|{x['id'] for x in extra},offset))
    random.Random(seed).shuffle(extra)
    return result + extra[:15]


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise SourceError("A public source attempted an insecure redirect.")
        result = super().redirect_request(request, response, code, message, headers, url)
        if result:
            result.remove_header("Authorization")
        return result


def download(record: dict, directory: Path) -> Path:
    parsed = urllib.parse.urlsplit(record["url"])
    if parsed.scheme != "https" or parsed.hostname not in {"archive.org", "upload.wikimedia.org", "storage.googleapis.com", "raw.githubusercontent.com"}:
        raise SourceError("Unapproved public source origin.")
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
    name = hashlib.sha256(record["url"].encode()).hexdigest()[:24] + suffix
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    algorithms = [x for x in ("md5", "sha1", "sha256") if record.get(x)]
    if not algorithms:
        raise SourceError("A public source needs a published checksum.")
    def check(path):
        if record.get("bytes") and path.stat().st_size != record["bytes"]:
            return False
        with path.open("rb") as stream:
            hashes = {x: hashlib.new(x) for x in algorithms}
            while chunk := stream.read(1024 * 1024):
                for digest in hashes.values(): digest.update(chunk)
        return all(hashes[x].hexdigest() == record[x] for x in algorithms)
    if target.exists():
        if check(target):
            return target
        raise SourceError("Cached source differs from its published checksum; it was not overwritten.")
    pending = target.with_suffix(target.suffix + ".partial")
    maximum = record.get("bytes") or 64 * 1024**2
    try:
        request = urllib.request.Request(record["url"], headers={"User-Agent": AGENT})
        with urllib.request.build_opener(SecureRedirect()).open(request, timeout=60) as response, pending.open("wb") as stream:
            if response.status != 200:
                raise SourceError("A complete public source download is required.")
            count, started = 0, time.monotonic()
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > maximum or time.monotonic() - started > 1800:
                    raise SourceError("Public source exceeded its size or transfer deadline.")
                stream.write(chunk)
        if not check(pending):
            raise SourceError("Downloaded source failed its published checksum or size check.")
        pending.replace(target)
        return target
    except (OSError, urllib.error.URLError):
        raise SourceError("Public media transfer failed; no signed redirect URL was logged.") from None
    finally:
        pending.unlink(missing_ok=True)
