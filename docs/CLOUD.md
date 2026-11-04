# Daily content and cloud review

**Every new day needs a new sound idea, new photographic backdrop, and new
thumbnail.** The Quiet Hours brand remains consistent; the Night Rain veranda
does not become a different video merely by changing its title or colors.

The current route is cloud rendering followed by **manual YouTube Studio upload**.
No Google Cloud project or OAuth connection is needed. The user has authorized
daily Filebin delivery, including each concept's titles and descriptions, and
selected **18:00 Asia/Nicosia**. That delivery target is recorded in
`config/delivery-settings.json`; it is not an active schedule. No daily cloud
service has been deployed by saving these files. YouTube publication stays manual.

The readiness check on 4 November 2026 found invalid configured GitHub
authorization, an empty remote repository, and no deployed scheduler. Restore
valid repository authorization, deploy and test the workflow, and provide the
remaining verified assets before enabling unattended delivery. No further time
selection is needed. A scheduled start at 18:00 would not guarantee files are
ready at 18:00; production must complete ahead of that delivery target.

## What changes each day

`config/daily-concepts.json` is the dated content queue. It distinguishes sound
families, settings, framing, backdrop ideas and thumbnail briefs across rain,
calming wildlife, shore water, seaside ambience and other relaxing scenes.
`config/brand-style.json` and [the channel style guide](CHANNEL-STYLE.md) define the
shared thumbnail design. Reuse that design, not yesterday's photograph.

The queue begins on its recorded `rotation_start_date`. When its last concept is
used, the planner stops and asks for more ideas; it never wraps back to the first
scene. A planned concept is **not** evidence that its recordings, permissions or
finished media already exist.

```bash
python3 daily.py plan --date 2026-11-04 \
  --output config/episodes/2026-11-04.json
```

Omit `--date` for today's date in Asia/Nicosia. The same command reuses an existing
matching brief without overwriting asset or review notes. A changed concept needs
a separately reviewed output. The brief initially marks all assets unverified
and leaves recording credits and disclosure decisions unset.

## Before a production render

For each brief, prepare and review:

- **Audio:** genuine field recordings matching the day's sound idea, with actual
  creator/source/license evidence and a passed audio quality report. A quiet
  shoreline needs water lapping; synthesized noise is not a substitute.
- **Backdrop:** a new photographic scene with its own `scene_id`. AI-created
  photographic scenes are allowed when described truthfully as AI-created, with
  their generation/provenance record and visual QA. They are not real location
  photographs or proof of where the sound was recorded.
- **Thumbnail:** new artwork from that scene, using the same brand layout and
  typography. Its `source_scene_id` must match the new backdrop.

Each `assets.audio`, `assets.backdrop` and `assets.thumbnail` record needs
`status: "verified"`, `path`, `sha256`, `kind`, actual `credits`,
`rights_verified: true`, `rights_evidence_path`, `qa_passed: true`, and
`qa_report_path`. Paths are relative to the episode JSON unless absolute. Kinds
are `real_field_recording` for audio, `photographic_ai_scene`/`photograph`/`video`
for the backdrop, and `branded_thumbnail` for the thumbnail. Set the episode's
`thumbnail_path` to the same verified thumbnail. Complete the title, description,
credits, audience/disclosure decisions and two reviewed Short moments.

These flags record an actual review; do not set them just to bypass a failed
check. The tool checks evidence files and hashes, but cannot decide copyright
permission or evaluate an aesthetic choice on its own.

```bash
python3 daily.py check-assets \
  --episode config/episodes/2026-11-04.json \
  --asset-history outputs/daily-asset-history.json

python3 daily.py prepare \
  --episode config/episodes/2026-11-04.json \
  --source-mode verified-assets \
  --asset-history outputs/daily-asset-history.json \
  --output-dir outputs/2026-11-04-quiet-shore
```

The retained nonsecret history rejects a soundtrack, backdrop, thumbnail, or
scene ID already assigned to another daily bundle. It was seeded locally with
the previous Night Rain tests so they cannot silently become the next day's new
content. Preserve this history with the assets when moving to a new cloud
machine; do not recreate a blank history on each run. File hashes detect exact
reuse; the separate visual review checks that a scene is not just a retitled or
recolored copy.

Every new daily MP4 requires visible scene motion, including both Shorts.
The still backdrop remains a design/image asset; it cannot be the daily render
input. Prepare a moving video and set the episode's `moving_visual` record with
`status: "verified"`, its `path`, `sha256`, matching backdrop `scene_id`, and
`qa_report_path`. The renderer repeats that reviewed moving clip as needed,
uses the real soundtrack, and creates one hour plus two distinct
15–30-second portrait Shorts.

The motion review JSON must identify `visual_sha256` and `scene_id`, record
`subject_motion_visible`, `camera_fixed`, and `matches_concept` as true after
actual playback review, and include concrete `review_notes`. Clips shorter than
one hour also require `loop_seam_reviewed: true`. These are recorded observations,
not a claim that FFprobe can judge whether waves or rain look natural. Check the
completed long export and both final vertical crops: water should visibly wash
in and recede, rain should fall and strike surfaces, and foliage/water should
move appropriately in wildlife scenes. Camera pans, zooms, captions and light
changes alone do not satisfy this requirement. Do not set review flags without
performing those checks.
Audio repetition still requires explicit `--loop-audio`; it is never silently
enabled. The output bundle includes the new branded thumbnail, suggested Short
cover frames and finished upload details. FFprobe and full decode checks validate
the three exports. Same-bundle retries reuse matching source receipts and verified
renders instead of changing or overwriting them.

`create_original.py` and its synthetic illustrated presets remain available only
for comparison. `daily.py prepare --source-mode generate-original` requires an
explicit `--legacy-synthetic` opt-in and is not offered by the production cloud
workflow. Production briefs cannot take that path.

## What Cloud can do without your laptop

A cloud job can render assets already prepared and verified in its workspace.
The content queue is not an unattended artist or a recording service. Image
generation available in this chat does not imply a free image-generation API in
GitHub Actions. Finding/licensing recordings, creating new scenes and thumbnails,
and reviewing them remain preparation steps until a separate verified automation
is approved.

The optional `.github/workflows/ambience-review.yml` has **manual dispatch only**.
It has no cron or automatic publishing. Its standard Ubuntu free-tier job skips
private repositories rather than assuming free minutes remain; it never changes
repository visibility. Keep your repository private unless you choose otherwise.
The current Codex Cloud workspace can still prepare files without making it public.

Its `deliver_to_filebin` option sends each completed concept's three videos,
thumbnail, suggested covers and exact publishing copy together. Set
`filebin_backdrop` to the corresponding scene image; this is required for a
complete daily Filebin package. The upload list is
fixed; local audit documents and credentials are not included. A separate
Filebin receipt branch preserves upload/download progress across runner retries.
See [Filebin delivery](FILEBIN-DELIVERY.md) for the command, retained-state rules
and the visibility of receipts in a public repository. The workflow summary
links to the finished concept folder and reports its actual expiry.
The option is off by default and explicitly discloses that receipt/download
URLs are stored publicly in this public repository. Choosing that option is a
separate visibility decision from approving Filebin delivery alone. The current
chat's uploads do not create those public GitHub receipts.

Before using that optional workflow, its trusted branch must contain the code,
and the runner must have the verified assets, episode and retained history at
the declared paths. The workflow does not invent or fetch missing production
assets. Choose `verified-assets` only after that preparation. `reuse-long` remains
available to reopen an existing verified episode under the same bundle ID.

The existing €0 guards remain: the `youtube-review` environment needs
`ZERO_SPEND_CONFIRMED=true` only after paid Actions/storage use is blocked at the
account. Review artifacts are optional, off by default, capped at 400 MiB and kept
one day. Account-wide free storage must still be available; do not purchase more
when a quota check fails. Artifacts in a public code repository are not a private
media vault. The workflow has not been run or deployed merely because it exists.

## Review in the browser

Download the prepared review bundle from the current cloud delivery link or a
successfully created workflow artifact. In [YouTube Studio](https://studio.youtube.com/),
select `@QuietHoursRelaxingSounds`, then **Create → Upload videos**. Use the finished
metadata and actual credits in `upload-details.md`, the new thumbnail, and the
reviewed disclosures. Save the tests as **Unlisted** and record each Studio link
and video ID before retrying, so existing uploads can be reused.

Set each Short's **Related video** to the full upload and select its suggested
cover frame where Studio/the YouTube app offers that setting. Review and publish
the full video first, then the Shorts. The current manual route cannot inspect
Studio visibility or capture IDs automatically.

Optional API tools remain in the repository for a future explicit choice; leave
`stage_uploads` and `apply_playlist` off. [YouTube API setup](YOUTUBE.md) documents
their secret store and channel checks. No credentials are required for the
current manual route. The confirmed daily delivery target is 18:00 Asia/Nicosia.
A fixed UTC cron would not preserve that local time through daylight-saving
changes. Enable a timezone-aware schedule only after deployment and its first
complete prepared-media delivery have been verified.
