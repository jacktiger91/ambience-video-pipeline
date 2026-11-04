# Quiet Shore — a new daily episode

Quiet Shore replaces the repeated porch composition with a fictional pebble
cove, a small distant beach hut, and open water at blue hour. The channel's
graphic style stays consistent: tall condensed cream lettering and a rounded
gold **1 HOUR** badge. The long-video backdrop remains text-free.

Three real stereo coastal recordings are arranged in four unique passages.
These are new sources, independent of Night Rain. The official Radio Aporee
archive supplies item-specific **CC BY 3.0** licences, creator details and field
descriptions. Attribution, source/licence links and notices of editing are in
[the recording credits](provenance/quiet-shore/CREDITS.md). The scene is disclosed
as AI-created, not camera footage from the recording locations.

Source selection excluded detected distant speech, isolated impacts and a
possible boat. No source interval is repeated. The hour uses 16-second
crossfades, floating-point intermediate audio, measured constant gain and
gentle peak protection. The final 3600-second, 48 kHz stereo master measured
−22.0 LUFS integrated, −4.3 dBTP and 8.6 LU loudness range.

Water animation is confined to a shoreline mask. The hut, rocks, land, sky,
horizon and camera stay fixed. A seamless 120-second visual cycle is reused for
efficient encoding; the audio is continuous and is never looped. Four renderer
tests and independent code review passed, including a real 12-second encode
and full decode. Four generic mixer tests passed, including actual stereo
render/decode, repeat-run reuse and overwrite protection.

## Reproduce from retained verified assets

Use Python 3.11+, FFmpeg/FFprobe, and the pinned Pillow dependency in
`requirements-animation.txt`. The source downloads, graphics and outputs stay
outside Git. All work runs in Cloud without a Windows machine.

```bash
python3 mix_field_recordings.py \
  --sources-manifest docs/provenance/quiet-shore/sources.json \
  --plan config/quiet-shore-audio-plan.json \
  --output media/quiet-shore/soundtrack.flac
python3 animate_water.py --scene media/quiet-shore/scene.png \
  --audio media/quiet-shore/soundtrack.flac \
  --output outputs/quiet-shore/long-source.mp4 --duration 3600 \
  --geometry outputs/quiet-shore/scene-geometry.json
python3 prepare_bundle.py --episode config/quiet-shore.json \
  --output-dir outputs/quiet-shore/review
```

Matching verified files are reused rather than regenerated. The full-hour
export's end-to-end decode reached 3600.000 seconds, at 1920×1080/30 fps with
H.264 video and stereo AAC. Its size is 994,361,989 bytes and SHA-256 is
`370ed6be73906e9e4ca920012a37bc5417df9b9a01dbe3b976be43d9e236bc80`.
Final bundle proofs and encoded-audio measurements are retained with the files.
These checks are technical and model-based; they do not claim subjective
listening by the assistant.

## Daily variety and handoff

The [brand guide](CHANNEL-STYLE.md) and fourteen dated production briefs cover
4–17 November 2026, Asia/Nicosia. The remaining concepts are plans awaiting
their own verified media. They are not completed videos. The queue stops at
its end instead of cycling silently. The daily asset guard compares actual
hashes and scene IDs against retained history, including both earlier tests.

YouTube upload and publication remain manual. The finished bundle includes
copyable titles, descriptions, recording credits, disclosure flags, thumbnail,
Short cover suggestions and Studio steps. Daily scheduling is disabled pending
the completed test and an agreed Asia/Nicosia start time. No paid external
service, environment publication or Git push was used for this episode.
