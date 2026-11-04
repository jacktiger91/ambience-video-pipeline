#!/usr/bin/env python3
"""Create original illustrated, synthesized ambience entirely in the cloud.

Only Python's standard library and FFmpeg/FFprobe are used. The FFmpeg build must
include librsvg (the Debian/Ubuntu package does). Audio is synthesized continuously
for the requested duration; no recorded or short looping soundtrack is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

from render import RenderError, parse_duration, require_tools, verify_export


THEMES = {
    "night-rain": {"title": "Quiet Night Rain", "sky": "#101d34", "horizon": "#365568", "warm": "#e9a66a", "noise": "white", "high": 180, "low": 6200, "period": 79},
    "coastal-dusk": {"title": "Coastal Dusk", "sky": "#262844", "horizon": "#b17f79", "warm": "#dca170", "noise": "pink", "high": 100, "low": 5400, "period": 17},
    "forest-mist": {"title": "Forest Mist", "sky": "#162e32", "horizon": "#688b84", "warm": "#c8b98e", "noise": "pink", "high": 250, "low": 4800, "period": 47},
    "cabin-rain": {"title": "Warm Cabin Rain", "sky": "#172a32", "horizon": "#5c7474", "warm": "#edac62", "noise": "white", "high": 300, "low": 7600, "period": 61},
    "river-evening": {"title": "River at Evening", "sky": "#24293c", "horizon": "#7b7489", "warm": "#dfa474", "noise": "white", "high": 100, "low": 4500, "period": 31},
    "winter-wind": {"title": "Quiet Winter Wind", "sky": "#243145", "horizon": "#8d9da6", "warm": "#d7ac83", "noise": "pink", "high": 70, "low": 2200, "period": 53},
    "distant-storm": {"title": "Distant Storm", "sky": "#121b2b", "horizon": "#3c4e64", "warm": "#dba67d", "noise": "pink", "high": 35, "low": 3200, "period": 97},
}
RECIPE_VERSION = 1
CREDITS = (
    "Original procedural illustration and synthesized ambience soundscape created "
    "for Quiet Hours Relaxing Sounds using Python and FFmpeg. "
    "No field recordings, music, or third-party media used."
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ffmpeg(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-filter_threads", "2", "-filter_complex_threads", "2", *arguments],
        check=True, capture_output=capture, text=capture,
    )


def make_scene(theme: str, seed: int) -> str:
    """Produce an original SVG illustration with deterministic, seeded detail."""
    rng = random.Random(seed)
    palette = THEMES[theme]
    rain = theme in {"night-rain", "cabin-rain", "distant-storm"}
    parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
<defs>
 <linearGradient id="room" x2="0" y2="1"><stop stop-color="#10151e"/><stop offset="1" stop-color="#20232a"/></linearGradient>
 <linearGradient id="sky" x2="0" y2="1"><stop stop-color="{palette['sky']}"/><stop offset="1" stop-color="{palette['horizon']}"/></linearGradient>
 <linearGradient id="lake" x2="0" y2="1"><stop stop-color="{palette['horizon']}"/><stop offset="1" stop-color="#122735"/></linearGradient>
 <linearGradient id="glass" x2="1" y2="1"><stop stop-color="#c2d4dd" stop-opacity=".07"/><stop offset=".5" stop-color="#c2d4dd" stop-opacity="0"/><stop offset="1" stop-color="#9db6c9" stop-opacity=".1"/></linearGradient>
 <linearGradient id="wood" x2="0" y2="1"><stop stop-color="#4e4140"/><stop offset="1" stop-color="#211f26"/></linearGradient>
 <radialGradient id="lamplight"><stop stop-color="{palette['warm']}" stop-opacity=".34"/><stop offset="1" stop-color="{palette['warm']}" stop-opacity="0"/></radialGradient>
 <radialGradient id="moonlight"><stop stop-color="#dfded1" stop-opacity=".32"/><stop offset="1" stop-color="#dfded1" stop-opacity="0"/></radialGradient>
 <clipPath id="outside"><rect x="244" y="112" width="1432" height="688" rx="16"/></clipPath>
</defs>
<rect width="1920" height="1080" fill="url(#room)"/>
<path d="M0 65H1920M0 920H1920" stroke="#393c41" stroke-width="3" opacity=".25"/>
<rect x="213" y="80" width="1494" height="760" rx="24" fill="#080e16" stroke="#353944" stroke-width="5"/>
<g clip-path="url(#outside)">
 <rect x="244" y="112" width="1432" height="688" fill="url(#sky)"/>
 <ellipse cx="1260" cy="254" rx="250" ry="215" fill="url(#moonlight)"/>
 <circle cx="1260" cy="254" r="43" fill="#c9d1cf" opacity=".57"/>
 <circle cx="1282" cy="237" r="44" fill="{palette['sky']}" opacity=".83"/>
''']
    for layer in range(3):
        points = [(220, 550 + layer * 40)]
        for x in range(220, 1725, 90):
            y = 440 + layer * 57 + rng.randrange(-45, 50)
            points.append((x, y))
        points.extend([(1740, 805), (220, 805)])
        parts.append(f'<polygon points="{" ".join(f"{x},{y}" for x,y in points)}" fill="{["#344956", "#263c48", "#1b303d"][layer]}"/>')
    parts.append('<path d="M244 684Q610 585 984 673T1676 633V800H244Z" fill="url(#lake)"/>')
    for _ in range(95):
        x, y = rng.randrange(260, 1660), rng.randrange(667, 800)
        length = rng.randrange(15, 160)
        parts.append(f'<path d="M{x} {y}h{length}" stroke="#adbdc7" stroke-width="1" opacity="{rng.uniform(.02,.11):.3f}"/>')
    for x in [260, 300, 345, 409, 455, 1510, 1564, 1627, 1676]:
        height = rng.randrange(140, 285)
        bottom = 780
        parts.append(f'<path d="M{x} {bottom-height}l{-height*.18:.0f} {height*.48:.0f}h{height*.10:.0f}l{-height*.20:.0f} {height*.32:.0f}h{height*.15:.0f}l{-height*.19:.0f} {height*.27:.0f}h{height*.64:.0f}l{-height*.19:.0f} {-height*.27:.0f}h{height*.15:.0f}l{-height*.20:.0f} {-height*.32:.0f}h{height*.10:.0f}Z" fill="#102430" opacity=".9"/>')
    for _ in range(12):
        x,y = rng.randrange(500,1450),rng.randrange(567,638)
        parts.append(f'<rect x="{x}" y="{y}" width="3" height="2" fill="#d8be8f" opacity=".45"/>')
    parts.append('<rect x="244" y="112" width="1432" height="688" fill="url(#glass)"/>')
    if rain:
        for _ in range(250):
            x,y = rng.randrange(250,1670),rng.randrange(120,790)
            length=rng.randrange(4,29)
            parts.append(f'<path d="M{x} {y}l-3 {length}" stroke="#b9cbd5" stroke-width="{rng.uniform(.7,1.8):.1f}" stroke-linecap="round" opacity="{rng.uniform(.06,.25):.2f}"/>')
        for _ in range(45):
            x,y = rng.randrange(250,1670),rng.randrange(120,790)
            parts.append(f'<ellipse cx="{x}" cy="{y}" rx="2" ry="4" fill="#cbd9de" opacity=".2"/>')
    elif theme == "winter-wind":
        for _ in range(95):
            x,y=rng.randrange(250,1670),rng.randrange(120,790)
            parts.append(f'<circle cx="{x}" cy="{y}" r="{rng.uniform(.6,2):.1f}" fill="#e4eceb" opacity=".4"/>')
    parts.append(f'''</g>
<path d="M955 110V805M243 488H1678" stroke="#15202b" stroke-width="17"/>
<path d="M960 112V800M244 492H1676" stroke="#59616a" stroke-width="2" opacity=".36"/>
<rect x="219" y="803" width="1483" height="39" rx="8" fill="url(#wood)"/>
<path d="M140 1040Q600 855 1660 949L1920 1080H0Z" fill="#24262d"/>
<ellipse cx="1590" cy="720" rx="320" ry="350" fill="url(#lamplight)"/>
<path d="M1430 952H1777L1812 985H1395Z" fill="#423739"/>
<path d="M1425 985V1080M1770 985V1080" stroke="#161923" stroke-width="20"/>
<ellipse cx="1610" cy="937" rx="69" ry="13" fill="#111723"/>
<path d="M1610 719V928" stroke="#79716a" stroke-width="8"/>
<path d="M1539 630H1681L1710 743Q1610 776 1510 743Z" fill="{palette['warm']}" opacity=".91"/>
<ellipse cx="1610" cy="743" rx="100" ry="15" fill="#f8d8a2" opacity=".8"/>
<ellipse cx="1610" cy="632" rx="71" ry="10" fill="#c59a76"/>
<path d="M525 930Q700 865 905 918L1042 1080H423Z" fill="#41444b"/>
<path d="M547 945Q696 885 883 937M570 973Q720 918 915 972M600 1010Q755 965 946 1015" fill="none" stroke="#767577" stroke-width="2" opacity=".24"/>
<path d="M700 1080L728 935M764 1080L784 929M829 1080L836 942" stroke="#1e2834" stroke-width="15" opacity=".3"/>
<ellipse cx="1230" cy="907" rx="85" ry="15" fill="#0f1823"/>
<path d="M1173 838H1283L1273 899Q1228 922 1184 898Z" fill="#9b9c96"/>
<ellipse cx="1228" cy="839" rx="55" ry="11" fill="#c2bba9"/>
<ellipse cx="1228" cy="841" rx="45" ry="7" fill="#3c302d"/>
<path d="M1281 849Q1327 845 1313 879Q1300 899 1279 883" fill="none" stroke="#9b9c96" stroke-width="12"/>
<path d="M1220 811Q1202 791 1222 770Q1236 754 1229 739M1243 815Q1254 794 1239 777" fill="none" stroke="#c9c3b4" stroke-width="3" opacity=".19"/>
<path d="M1032 917L1139 908L1165 938L1050 953Z" fill="#6f514c"/>
<path d="M1038 922L1139 914L1157 934L1053 946Z" fill="#bcad92"/>
<path d="M230 906Q198 767 150 660M226 851Q274 743 324 694M220 831Q169 724 126 736M226 876Q273 802 335 812" fill="none" stroke="#334944" stroke-width="8"/>
<ellipse cx="157" cy="691" rx="22" ry="55" transform="rotate(-32 157 691)" fill="#354f48"/>
<ellipse cx="295" cy="735" rx="24" ry="56" transform="rotate(43 295 735)" fill="#496258"/>
<ellipse cx="150" cy="761" rx="23" ry="51" transform="rotate(-60 150 761)" fill="#304b45"/>
<ellipse cx="301" cy="832" rx="21" ry="46" transform="rotate(58 301 832)" fill="#40594e"/>
<path d="M159 900H286L271 1007Q222 1026 174 1007Z" fill="#665451"/>
<ellipse cx="222" cy="901" rx="63" ry="14" fill="#927b69"/>
<ellipse cx="222" cy="901" rx="52" ry="9" fill="#282d2b"/>
<rect x="0" y="0" width="1920" height="1080" fill="none" stroke="#080e16" stroke-width="45" opacity=".35"/>
</svg>''')
    return "\n".join(parts)


def audio_graph(theme: str, seed: int, duration: float) -> str:
    palette = THEMES[theme]
    fade = min(3.0, duration / 4)
    gain = 0.88 if palette["noise"] == "white" else 1.8
    # Independent deterministic sources run for the complete export. The slow
    # gain drift changes the texture but never loops or restarts the waveform.
    return (
        f"anoisesrc=color={palette['noise']}:amplitude=0.44:sample_rate=48000:seed={seed}:duration={duration},"
        f"highpass=f={palette['high']},lowpass=f={palette['low']}[left];"
        f"anoisesrc=color={palette['noise']}:amplitude=0.44:sample_rate=48000:seed={(seed+7919)%(2**32)}:duration={duration},"
        f"highpass=f={palette['high']},lowpass=f={palette['low']}[right];"
        "[left][right]amerge=inputs=2,"
        f"volume='{gain}*(0.86+0.08*sin(2*PI*t/{palette['period']})+0.06*sin(2*PI*t/137))':eval=frame,"
        f"afade=t=in:d={fade},afade=t=out:st={max(0,duration-fade)}:d={fade},"
        "aformat=sample_rates=48000:channel_layouts=stereo[audio]"
    )


def verify_playthrough(path: Path, duration: float) -> dict:
    metadata = verify_export(path, duration)
    ffmpeg(["-xerror", "-threads", "4", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
    return {"duration": float(metadata["format"]["duration"]), "full_decode_passed": True, "sha256": sha256(path)}


def create(theme: str, seed: int, duration: float, output: Path, assets_dir: Path) -> dict:
    require_tools()
    if theme not in THEMES:
        raise RenderError(f"Unknown theme: {theme}")
    if output.suffix.lower() != ".mp4":
        raise RenderError("Output must end in .mp4")
    recipe = {"recipe_version": RECIPE_VERSION, "theme": theme, "seed": seed, "duration": duration}
    provenance_path = output.with_suffix(".provenance.json")
    if os.path.lexists(output):
        if not provenance_path.is_file():
            raise RenderError("Existing output has no provenance. Choose a new output path; no file was overwritten.")
        prior = json.loads(provenance_path.read_text())
        if prior.get("recipe") != recipe or prior.get("verification", {}).get("sha256") != sha256(output):
            raise RenderError("Existing output does not match this recipe/cache. Choose a new output path.")
        print(f"Reusing verified original: {output}")
        return prior
    output.parent.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    source_key = f"{theme}-{seed}-v{RECIPE_VERSION}"
    svg_path = assets_dir / f"{source_key}.svg"
    scene_path = assets_dir / f"{source_key}.png"
    svg_text = make_scene(theme, seed)
    if svg_path.exists() and svg_path.read_text() != svg_text:
        raise RenderError("Existing illustration differs from recipe; choose a clean assets directory.")
    if not svg_path.exists():
        svg_path.write_text(svg_text)
    with tempfile.TemporaryDirectory(prefix=".original-", dir=output.parent) as temporary_directory:
        temporary = Path(temporary_directory)
        # Always derive the raster from the verified SVG, so a changed PNG cannot
        # silently alter the final render. Publish the source PNG only if absent.
        image = temporary / "scene.png"
        ffmpeg(["-i", str(svg_path), "-frames:v", "1", "-threads", "2", str(image)])
        if scene_path.exists() and sha256(scene_path) != sha256(image):
            raise RenderError("Existing source PNG changed; choose a clean assets directory.")
        if not scene_path.exists():
            os.link(image, scene_path)
        clip = temporary / "visual.mp4"
        ffmpeg([
            "-loop", "1", "-framerate", "30", "-i", str(image), "-t", "10",
            "-vf", "scale=1920:1080:out_range=tv,setsar=1,format=yuv420p",
            "-c:v", "libx264", "-tune", "stillimage", "-preset", "medium", "-crf", "18",
            "-g", "300", "-keyint_min", "300", "-sc_threshold", "0", "-threads", "4",
            "-pix_fmt", "yuv420p", "-color_range", "tv", "-an", str(clip),
        ])
        encoded = temporary / "long.mp4"
        print(f"Synthesizing {duration:g}s of continuous stereo ambience for {theme}...", flush=True)
        ffmpeg([
            "-stream_loop", "-1", "-i", str(clip), "-filter_complex", audio_graph(theme, seed, duration),
            "-map", "0:v:0", "-map", "[audio]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-ac", "2", "-ar", "48000", "-threads", "4", "-t", f"{duration:.6f}",
            "-map_metadata", "-1", "-movflags", "+faststart", str(encoded),
        ])
        print("Checking all video frames and audio packets...", flush=True)
        verification = verify_playthrough(encoded, duration)
        provenance = {
            "schema_version": 1, "recipe": recipe, "title": THEMES[theme]["title"],
            "recording_credits": CREDITS, "contains_synthetic_media": True,
            "source_svg_sha256": sha256(svg_path), "source_png_sha256": sha256(image),
            "audio_method": "Two independent full-duration seeded noise sources, filtered and slowly modulated; no soundtrack repetition.",
            "visual_method": "Original procedural SVG illustration, held still throughout the hour; encoded visual stream is reused efficiently.",
            "verification": verification,
        }
        os.link(encoded, output)
        try:
            with provenance_path.open("x") as stream:
                json.dump(provenance, stream, indent=2)
                stream.write("\n")
        except BaseException:
            # Keep the verified completed video; a missing provenance should
            # fail safe on retry rather than deleting a successful render.
            raise
        print(f"Created and fully verified: {output}", flush=True)
        return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", choices=THEMES, default="night-rain")
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--duration", type=parse_duration, default=3600.0)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--assets-dir", type=Path, default=Path("media/originals"))
    args = parser.parse_args()
    if not 0 <= args.seed < 2**32:
        parser.error("--seed must be between 0 and 4294967295")
    try:
        create(args.theme, args.seed, args.duration, args.output.resolve(), args.assets_dir.resolve())
    except (RenderError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled; incomplete temporary render removed.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
