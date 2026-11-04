# Ambience video renderer

Render a moving visual and a finished ambience soundtrack into a YouTube-ready
MP4 using Python, FFmpeg, and FFprobe. The project also prepares two Shorts,
artwork and publishing copy, then delivers approved packages through Filebin.
Rendering and delivery run in Linux cloud environments; a Windows laptop is not
needed. The basic `render.py` command uses only Python's standard library and
FFmpeg/FFprobe. Optional artwork and archive helpers have their own dependencies.

YouTube upload, review and publication remain manual. Each daily episode must
have its recordings, rights, moving scene and thumbnail prepared and verified
before it enters the delivery queue.

## Three-video review workflow

Each new episode needs its own real stereo field-recording arrangement,
photographic backdrop, matching thumbnail and two distinct 15–30-second Shorts.
The channel keeps the [same cream-title/gold-badge design](docs/CHANNEL-STYLE.md)
while changing the soundscape, location and composition. Retitling the Night
Rain porch is not a new daily episode.

The [daily concept queue](config/daily-concepts.json) contains fourteen distinct
briefs, beginning with **Quiet Shore**. Create or reuse a dated brief with:

```bash
python3 daily.py plan --date 2026-11-04 \
  --output outputs/daily-briefs/2026-11-04.json
```

A brief is not a completed video. Actual audio permissions, downloaded source
bytes, distinct backdrop and thumbnail, credits, and quality checks are required
before production. The planner stops at the queue's end instead of recycling it.
Image-generation tools are available interactively here; the cloud job renders
prepared assets and does not silently call a paid image API.

Each concept includes a full-video title and description plus separate titles
and descriptions for both Shorts in
[the publishing-copy catalogue](config/daily-publishing-copy.json).
The dated brief carries this copy into the review bundle. Future copy is a draft:
check it against the finished recording and scene, append the actual credits
for each video, and select the Short moments before rendering. The readable
[14-concept copy pack](docs/DAILY-TITLES-DESCRIPTIONS.md) keeps those pending
credits separate from the text to paste into YouTube.

All new daily MP4s, including both Shorts, require **visible motion within the
scene**: shoreline water washing in and receding, falling rain and splashes, or
appropriate movement of water, reeds and foliage. The camera stays fixed.
The daily job requires a reviewed `moving_visual` video and its motion report;
it will not substitute the still backdrop image. See the
[motion review requirements](docs/CLOUD.md) before preparing a new episode.

Matching verified media is reused on retry. New episodes cannot reuse another
episode's scene, thumbnail or soundtrack hashes. Read the finished bundle's
`upload-details.md`, upload the three MP4s through Studio as **Unlisted**, and
review them before publishing the full video followed by the Shorts. Select
the playlist and each Short's Related video/cover in Studio.

The earlier synthesized test in `outputs/first-test/` and the photographic
[Night Rain revision](docs/REVISED-NIGHT-RAIN.md) remain available for comparison.
The synthetic generator is a legacy example, not the daily production route.
Animation helpers require Python 3.11+ and the pinned Pillow dependency in
`requirements-animation.txt`; the original renderer below remains standard-library-only.

For future cloud runs, see [cloud setup and the daily planner](docs/CLOUD.md).
The requested daily Filebin delivery target is **18:00 Asia/Nicosia**, including
each concept's titles, descriptions and recording credits with its finished
media. Repository-scoped GitHub access works, and source code is now on `main`.
The [daily workflow](.github/workflows/daily-filebin.yml) uses standard Ubuntu
runners in this public repository, with no paid runners, Actions media artifacts
or paid generation APIs. It selects the exact Cyprus date, begins attempts at
16:00, and reports whether delivery completed by 18:00. GitHub scheduling is best
effort, so the target is not a service guarantee.

[Scheduled-delivery configuration](config/scheduled-delivery.json) is the
activation and prepared-queue source of truth. While `enabled` is false, timed
workflow invocations do not deliver media. Enablement follows the private-draft
access check, connection test and prepared-media preflight; a successful text
probe alone does not prove full media delivery. Future concepts still need
production and QA. This queue does not create an indefinite supply of new
recordings or artwork.

Prepared packages, retry receipts and the daily review links belong in the
dedicated **draft** release. Open [the repository's Releases page](https://github.com/jacktiger91/ambience-video-pipeline/releases)
while signed into an account with repository write access. Keep that release a
draft; publishing it would expose its assets and review links. Public workflow
summaries show status and the review-page link, not the Filebin URLs.
Optional [YouTube API connection instructions](docs/YOUTUBE.md) are
available if you later choose free API registration; they are not required for
manual Studio uploads.

For each new, finished review bundle, Filebin delivery includes the three MP4s,
thumbnail, both suggested covers and a concept-specific `upload-details.txt`.
Include the backdrop image with `--backdrop` for a complete daily package. Keep the same state file on
retries so accepted files and complete download checks are reused:

```bash
python3 filebin_delivery.py --bundle outputs/NEW_BUNDLE/bundle.json \
  --state outputs/deliveries/NEW_BUNDLE.json \
  --backdrop media/NEW_CONCEPT/scene.png --approved-filebin
```

Use `--check-local-only` instead of `--approved-filebin` to inspect the exact
file list without network access. Existing Quiet Shore media is already
delivered; its new publishing-copy download links back to that set. The daily
workflow retains private Filebin receipts between runners; see
[delivery behavior and temporary-link limits](docs/FILEBIN-DELIVERY.md).

Run the extended offline checks as well as the renderer smoke test:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s tests -p 'smoke_test.py' -v
```

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
