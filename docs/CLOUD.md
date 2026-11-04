# Daily content and cloud review

**Every new day needs a new sound idea, new photographic backdrop, and new
thumbnail.** The Quiet Hours brand remains consistent; the Night Rain veranda
does not become a different video merely by changing its title or colors.

The current route is cloud rendering followed by **manual YouTube Studio upload**.
No Google Cloud project or OAuth connection is needed. The user has authorized
daily Filebin delivery, including each concept's titles and descriptions, and
selected **18:00 Asia/Nicosia**. YouTube publication stays manual. The daily
workflow uses a prepared-media queue, with attempts beginning at 16:00 to leave
time for transfer and retries before the 18:00 target.

Repository-scoped Git and GitHub API access work, and source code is now on
`main`. The earlier generic CLI login check did not establish that repository
access was broken. Test the actual repository operation instead of requesting
new credentials based on `gh auth status` alone. Codex's GitHub upload-host
permissions can differ from repository API access; native GitHub Actions jobs
use their own scoped `GITHUB_TOKEN` for draft-release asset transfers.

`config/scheduled-delivery.json` records the activation flag, private draft and
exact dates with prepared assets. While `enabled` is false, scheduled workflow
invocations remain inert. A deployed workflow, an enabled selector and a verified
full delivery are separate readiness facts. No further time selection is needed.
GitHub cron timing is best effort; beginning the transfer at 18:00 would not make
the files ready by 18:00.

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

A cloud job can render assets already prepared and verified in its workspace,
or deliver an already-produced package while the laptop is off. The content
queue is not an unattended artist or a recording service. Finding/licensing
recordings, preparing moving scenes and thumbnails, and reviewing the results
remain preparation steps. Image generation in this chat does not imply a free
generation API in GitHub Actions. The queue stops when a date lacks its own
finished concept; it never substitutes yesterday's video.

The primary `.github/workflows/daily-filebin.yml` runs only in this named public
repository on standard `ubuntu-24.04` runners. It uses no paid larger runners,
Actions media artifacts or external paid APIs. It never changes repository
visibility. UTC trigger windows cover both Cyprus offsets; `daily_schedule.py`
uses `Asia/Nicosia` for the actual date, 16:00 attempt window and 18:00 deadline.
Late attempts retain the same date and report a missed deadline truthfully.

The dedicated `quiet-hours-private-delivery` **draft release** holds prepared
archives, immutable receipt versions and the private review page. Each archive
must be smaller than 2 GiB. It contains the six finished media files, backdrop,
portable bundle metadata and hash-bound final motion reviews. Assets remain out
of Git. Only the small date/asset-ID/size/hash mapping belongs in the source
configuration. The runner checks that the release is still the configured draft
before accessing it. Publishing that draft would expose its assets and links;
the runner refuses to continue if it has become a published release.

The daily workflow first restores the private receipt. A completed matching
delivery is reused; an incomplete delivery resumes only missing uploads/checks.
It validates the prepared archive and recorded media/motion evidence, then sends
the three MP4s, thumbnail, two covers, backdrop and concept-specific publishing
copy to Filebin. Receipt updates are persisted before subsequent transfers.
The release's private review notes receive the final Filebin link and expiry.
Public logs and summaries contain status and a link to the signed-in review
page, not private Filebin URLs or source assets.

Before enabling `config/scheduled-delivery.json`, verify the draft is inaccessible
to an anonymous request from an actual GitHub runner, complete the small approved
publishing-copy connection test, and preflight the intended full media package.
The manual modes have distinct purposes:

- `bootstrap-probe` imports a small approved text asset using the native runner
  credential. `connection-test` checks its Filebin upload/download and private
  retry receipt. Neither proves that an hour-long video has been queued.
- `bootstrap-episode` imports exactly eight already-authorized Filebin files.
  Portable bundle metadata, final motion evidence and the completed delivery
  receipt come from the private draft body, not a public source file or a
  Filebin audit-document upload. The worker builds and verifies the prepared
  archive in the private draft. This step does not create another Filebin
  media upload.
- `preflight` restores and checks the prepared archive and motion evidence,
  including when the same media already has a completed receipt.
- `delivery-test` is an explicit-date manual check of the real delivery path.
  It reuses completed matching receipts and updates the private review page.
  If files already exist, report that reuse instead of claiming a new full
  transfer. This mode does not enable the schedule.

For example, after the Forest Dawn package is mapped to its date:

```bash
gh workflow run daily-filebin.yml --repo jacktiger91/ambience-video-pipeline \
  -f mode=preflight -f episode_date=2026-11-05
gh workflow run daily-filebin.yml --repo jacktiger91/ambience-video-pipeline \
  -f mode=delivery-test -f episode_date=2026-11-05
```

Inspect the actual run results before enabling the queue. Record private access,
text connectivity, prepared-media restoration and final delivery/reuse as
separate checks. A successful text transfer alone does not establish readiness
of the full daily package.

The older `.github/workflows/ambience-review.yml` remains a manual preparation
and optional YouTube-API route. Its opt-in Filebin branch-ledger mode stores
download URLs publicly and is not the private daily route. Its existing
`ZERO_SPEND_CONFIRMED` and optional artifact-storage gates remain; do not enable
paid usage or assume those gates have been satisfied. See
[Filebin delivery](FILEBIN-DELIVERY.md) for the distinct receipt backends.

## Review in the browser

Open [the repository Releases page](https://github.com/jacktiger91/ambience-video-pipeline/releases)
while signed into an account with repository write access, then open the
`quiet-hours-private-delivery` draft. Its notes link to each successfully
delivered concept. Download the complete Filebin package before its reported
expiry. In [YouTube Studio](https://studio.youtube.com/),
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
The local-date selector handles daylight-saving changes. Keep the queue supplied
with verified packages and inspect delivery status; neither a source-code push
nor a configured cron proves that the next episode is ready.
