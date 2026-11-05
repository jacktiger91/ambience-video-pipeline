# Quiet Hours ambience production

Create a new daily nature ambience package in the cloud, with a ready-by target
of **18:00 Asia/Nicosia** and a **€0 service budget**. YouTube upload, review and
publication remain manual. The personal computer can be switched off.

Each package contains:

- One hour of real stereo field recordings and a fixed, naturally moving 1080p scene.
- Two distinct 15–30-second vertical Shorts at 1080 × 1920, with ambience audio and an invitation to the full hour.
- A matching photographic backdrop and thumbnail in the cream-title/gold-badge channel style, plus two suggested Short covers.
- Separate titles and descriptions for all three videos, actual source credits, licence links and manual Studio settings.

[Daily production](.github/workflows/daily-production.yml) discovers fresh
commercially permitted sources, screens the sound and motion, makes the artwork
and publishing copy, and renders a day ahead. It uses actual camera footage;
water, rain or foliage moves within the scene. The visual clip repeats with a
brief dissolve. The separately recorded soundtrack uses distinct passages with
smooth transitions, never a short repeating audio loop.

[Daily delivery](.github/workflows/daily-filebin.yml) starts at 12:00 Cyprus time
and retries before the 18:00 target. Both jobs use standard free Linux runners
in this public repository; no paid API, Google Cloud account or local laptop
service is required. GitHub schedules and Filebin availability are best effort,
so the ready-by time is a target rather than a guaranteed service deadline.

Open [the repository Releases page](https://github.com/jacktiger91/ambience-video-pipeline/releases)
while signed in as the owner. Each dated **private draft** holds its package,
quality evidence and final Filebin download link. Keep these drafts unpublished.
Filebin links are temporary; download before the expiry shown with each package.
Public workflow summaries contain status and this signed-in review-page link.

See [cloud operation and recovery](docs/CLOUD.md) and
[actual deployment/test status](docs/DEPLOYMENT-STATUS.md). Activation is explicit
in [production settings](config/production-settings.json) and
[delivery settings](config/scheduled-delivery.json); a source-code push alone
is not evidence of a successful production run.

Quality gates check explicit CC0/CC BY permission, actual stereo separation,
continuous audio-event screening, nonoverlapping source passages, measured
loudness, scene motion, stream specifications and full decoding of all three
exports. These are automated checks, not a claim of human listening. A failed
source or check blocks that dated package and produces a failed workflow run.
Earlier packages stay available; retries reuse verified bytes and receipts.

The [original fourteen concept briefs](config/daily-concepts.json),
[publishing-copy catalogue](config/daily-publishing-copy.json) and `daily.py`
remain available for manually curated episodes. Automatic production selects
fresh sources and a dated identity beyond those initial briefs. It excludes
previously used source IDs and hashes using private production history.

The standalone renderer below still uses Python's standard library and FFmpeg.
For the complete producer, create a Python 3.12 virtual environment and install
`requirements-production.txt`; the workflow performs this setup automatically.
Its local audio classifier is free and uses the pinned official YAMNet model.
Bebas Neue is bundled under the [SIL Open Font License](assets/fonts/OFL.txt).

## Manual review

Upload the three MP4s in YouTube Studio with the included `upload-details.txt`,
thumbnail and credits. Save as Unlisted, choose the playlist and disclosures,
set each Short's Related video, and select its suggested cover frame. Review and
publish the full hour first, then the Shorts. This project does not connect to
or publish to the channel automatically.

## Setup in Codex Cloud

Use the existing checkout; cloud tasks already run in an isolated environment.

```bash
cd /workspace/ambience-video-pipeline
python3 --version
ffmpeg -version
ffprobe -version
```

Requirements are Python 3.10 or newer and FFmpeg/FFprobe on `PATH`. FFmpeg must
include the `libx264` and `aac` encoders. The usual Debian/Ubuntu FFmpeg package
provides them. If the tools are missing, install them in the cloud machine:

```bash
sudo apt-get update
sudo apt-get install -y python3 ffmpeg
```

Omit `sudo` when running as root. Package installation requires access to the
distribution's package repositories. Rendering and the smoke test work offline
after installation.

No `pip install` or virtual environment is needed. Confirm the CLI is available:

```bash
python3 render.py --help
mkdir -p media outputs
```

Place source media in `media/` using the cloud workspace. The examples below
assume files named `media/scene.jpg`, `media/scene.mp4`, and
`media/soundtrack.wav`; substitute your own paths. Quote paths containing spaces.

## Render a preview

Create a 30-second preview from a still image:

```bash
python3 render.py \
  --visual media/scene.jpg \
  --audio media/soundtrack.wav \
  --duration 30 \
  --output outputs/preview-image.mp4
```

Use a video clip instead:

```bash
python3 render.py \
  --visual media/scene.mp4 \
  --audio media/soundtrack.wav \
  --duration 00:00:30 \
  --output outputs/preview-video.mp4
```

The still is held for the export. A video clip repeats as needed, and its own
audio is ignored. The separate soundtrack supplies all exported audio. Visuals
retain their aspect ratio and are centered with black padding to fill 1920 ×
1080. Repeated clips are simple repeats; no crossfade or seamless-loop editing is
added.

## Render a full hour

For an hour-long export, supply a soundtrack that is at least an hour long:

```bash
python3 render.py \
  --visual media/scene.mp4 \
  --audio media/soundtrack.wav \
  --duration 01:00:00 \
  --output outputs/ambience-1h.mp4
```

Omitting `--duration` also produces one hour. A longer soundtrack is trimmed to
the requested duration. A shorter soundtrack produces a clear error by default.
Choose a longer track or a shorter export to avoid recognizable audio repeats.

If repeating the soundtrack is intentional, opt in explicitly:

```bash
python3 render.py \
  --visual media/scene.jpg \
  --audio media/soundtrack.wav \
  --duration 01:00:00 \
  --loop-audio \
  --output outputs/ambience-1h-looped-audio.mp4
```

Audio loops are repeated as supplied, with no crossfade or automatic seam repair.
Listen to a preview around the repeat boundary before choosing this option for
the channel.

## Arguments and output

| Argument | Meaning |
| --- | --- |
| `--visual PATH` | Required still image or video clip. Common still formats include JPEG and PNG; video support depends on the installed FFmpeg decoders. |
| `--audio PATH` | Required separate finished soundtrack containing an audio stream. |
| `--duration VALUE` | Seconds, such as `30` or `2.5`, or `HH:MM:SS`, such as `00:05:00` or `01:00:00`. Fractional seconds are also accepted in the clock format. Default: `3600`. |
| `--output PATH` | Required destination ending in `.mp4`. Parent directories are created as needed. |
| `--loop-audio` | Explicit permission to repeat a soundtrack that is too short. |
| `--help` | Show CLI usage and options. |

Durations must be finite and positive, with at least one frame at 30 fps. Existing
outputs are never overwritten; use a new output filename for each render.

Every export uses these settings:

- 1920 × 1080, 30 fps H.264 video with the `libx264` encoder and `yuv420p` pixels.
- CRF 18 and the `medium` encoding preset; still images use the `stillimage` tune.
- Stereo AAC audio at 192 kbit/s and 48 kHz.
- MP4 fast start for playback before a download finishes.

FFmpeg reports progress on standard error. Full-hour rendering can take time;
CPU, source complexity, and available disk space affect the result. The video
encoder is limited to four threads. No GPU is required.

The renderer validates inputs before encoding, renders to a temporary file beside
the destination, and uses FFprobe to verify the result before creating the final
output. Existing files remain protected even if another process creates the
destination during the render. Failed renders and Ctrl+C cancellation clean up
their temporary output. A forced process kill or machine shutdown can leave a
temporary file beside the destination. Leave enough free disk space for the
complete export.

Verification checks the expected video and audio streams, resolution, codecs,
stereo audio, and duration. Container and stream durations can differ from the
request by up to 0.1 seconds because video frames and AAC packets have discrete
lengths. To inspect an export yourself:

```bash
ffprobe -v error \
  -show_entries stream=codec_name,codec_type,width,height,pix_fmt,channels,sample_rate,duration:format=duration \
  -of json outputs/preview-image.mp4
```

## Smoke test

From the repository root, run:

```bash
python3 -m unittest discover -s tests -p 'smoke_test.py' -v
```

The test generates short temporary image, video, and audio fixtures, invokes the
actual CLI, and checks the resulting files with FFprobe. It exercises both visual
types, soundtrack trimming and explicit looping, input validation, and overwrite
protection. Fixtures and outputs are removed afterward. The short test validates
the rendering workflow without performing a full-hour export.

## Keep media out of Git

`.gitignore` excludes `media/`, `outputs/`, `renders/`, `tmp/`, common media file
extensions, temporary files, and Python caches. Source code, documentation, and
tests belong in Git; source media and exports do not. Keep media in these ignored
directories and do not force-add it. Ignore rules do not remove a file that was
already tracked.
