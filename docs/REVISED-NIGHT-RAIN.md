# Revised Night Rain: garden veranda

This revision preserves the earlier illustrated/synthesized draft in
`outputs/first-test/`. It uses a new photographic-style AI-created veranda scene,
animated outdoor rain, and real stereo rain field recordings. The visual is not
presented as a camera recording of a real location.

## Recording permissions and edit plan

The source manifest is `docs/provenance/revised-night-rain/sources.json`.
Per-source evidence files preserve the creator's recording description, source
URL, and CC0 licence statement. The source files are full-length, publicly
served high-quality MP3 previews; downloading original uncompressed versions
would require a Freesound login. Each downloaded file has a recorded SHA-256,
actual duration, sample rate and channel count. Only verified stereo files enter
the hour.

The one-hour arrangement uses different source intervals once each, with
16-second equal-power crossfades. No source interval or short soundtrack is
looped. The mix preserves naturally varying rain and real recorded rumbles,
matches passage levels, and is measured for loudness and peak headroom. Source
and transition samples are retained in `outputs/revised-night-rain/audio-audit/`.
See the mix's provenance JSON for the exact edit decision list and measurements.

The attached five-minute trial supplied useful source leads. Its metadata alone
was not treated as permission evidence. The source credited as Straal99/623847
was excluded because the original is mono.

## Reproduce in Cloud

The original renderer needs only Python and FFmpeg. The added animation requires
Python 3.11+ and Pillow, with no paid API or external rendering service:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-animation.txt
source .venv/bin/activate
```

The current Codex cloud machine already has the tested Pillow version. Downloaded
recordings and generated scene/thumbnail assets stay in ignored `media/` paths.
Keep the provenance documents with the source material if moving the workflow.

```bash
python3 mix_real_rain.py --sources-manifest docs/provenance/revised-night-rain/sources.json \
  --output media/revised-night-rain/soundtrack.flac
```

Use the final mix and its automatically checked thunder cue file with `animate_rain.py`. Its
help lists the explicit scene, audio, output and cue arguments. The camera stays
fixed. Rain has three depth/speed layers, matched incoming drops and ground
impacts, perspective ripples, and shelter masks. A seamless visual cycle is
reused for efficient encoding; the audio is never looped. Sky glows are added
only at supplied rumble cues and precede the associated onset by about 0.35s.
Without verified cues, no glow is added.

For the retained scene and final cue file:

```bash
python3 animate_rain.py --scene media/revised-night-rain/scene.png \
  --audio media/revised-night-rain/soundtrack.flac \
  --output outputs/revised-night-rain/long-source.mp4 --duration 3600 \
  --geometry outputs/revised-night-rain/scene-geometry.json \
  --thunder-cues outputs/revised-night-rain/audio-audit/cues.json
python3 prepare_bundle.py --episode config/revised-night-rain-veranda.json \
  --output-dir outputs/revised-night-rain/review
```

The revised bundle uses a new stable bundle ID. `prepare_bundle.py` accepts a
finished `thumbnail_path`, normalized to a 1280×720 JPEG, and optional
`crop_position` values from zero to one for each Short. Thumbnail content and
Short recipes participate in reuse validation, so changes cannot silently reuse
old assets.

## Review and connection status

All three exports must pass FFprobe checks and full video/audio decode. The
exported AAC audio also needs a final loudness/peak check. Automated signal or
audio-event checks are not described as subjective listening. The user reviews
the finished media before publication.

Filebin delivery and YouTube OAuth are separate connections. At the start of
this revision, this Cloud instance had no usable YouTube OAuth client/refresh
grant or previous YouTube video IDs. Do not claim Studio staging, channel-ID
verification, live token refresh, or actual unlisted visibility without real
API results. If an existing connection is supplied securely, use `youtube.py`
with the new bundle ID; it records each upload ID and preserves the older draft.

The API cannot set Shorts Related video or select a Shorts cover frame. These
remain explicit Studio/app steps. The playlist is also pending the user's
selection. Neither automatic public visibility nor a daily schedule is enabled.
