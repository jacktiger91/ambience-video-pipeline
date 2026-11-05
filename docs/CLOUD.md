# Daily production and cloud review

The agreed target is **18:00 Asia/Nicosia**, including daylight-saving changes.
Standard free Ubuntu runners in this public repository do all production and
Filebin delivery. No laptop, paid API, Google Cloud project or YouTube OAuth
connection is needed. YouTube uploads, review and publication stay manual.

## Production

`daily-production.yml` runs at 00:13, 06:13 and 10:13 UTC. Each invocation checks
today's package and prepares tomorrow's if needed. A concurrency group prevents
competing producers. `config/production-settings.json` controls activation and
start date. Manual dispatch accepts an explicit test/recovery date.

The producer rotates between shore, meadow, stream, rain, birds and freshwater,
avoids consecutive identical sound families and excludes previously used source
IDs and hashes. It discovers original field recordings in Radio Aporee's
Internet Archive collection and HD camera footage from identified creators or
Wikimedia Commons. Only explicit CC0 or compatible CC BY grants are accepted.
If one theme lacks eligible sources, another relaxing theme is researched;
the title and description describe the actual selected sources.

The pinned free YAMNet classifier screens the audio continuously. Speech,
music, disruptive sounds and microphone noise are excluded with margins.
Actual stereo separation is required. Long nonoverlapping passages fill an
hour after 16-second crossfades, with measured loudness and peak headroom.
No short audio loop, synthetic rain or generated noise fills missing sound.

HD footage must pass fixed-camera and subject-motion checks, including two
separate vertical crops. A brief dissolve joins visual clip repeats. The
soundtrack is independent; the description explains that scene and audio were
recorded separately. The thumbnail uses that footage's frame with the channel's
cream condensed lettering and gold duration badge. All three exports must fully
decode and pass stream, duration, level and temporal-motion checks.

AAC can introduce peaks above those measured in the source soundtrack. An
export that exceeds the conservative limit receives a bounded constant-volume
correction while its encoded video is copied unchanged. The corrected MP4 is
fully decoded and measured again; the original export and passing Shorts remain
retained. The corrected file and its proof are checkpointed for reuse as well.

These checks are automated evidence, not human listening or a subjective
creative-quality guarantee. The user remains the final reviewer. Failed source
or quality checks block that date; yesterday's video is never substituted.

## Delivery and recovery

`daily-filebin.yml` starts at 12:00 Cyprus time and retries before 18:00. The
local-date selector handles both Cyprus offsets. Production happens ahead of
delivery to leave recovery time. GitHub timing and Filebin availability are
best effort; a missed deadline must be reported truthfully.

Each date uses a `quiet-hours-private-YYYY-MM-DD` **draft release** containing
the frozen plan, permissions, verified render checkpoints, final QA, complete
prepared archive and immutable delivery receipts. Annual private history drafts
retain used source identities. The runner checks the exact draft ID/tag and
requires anonymous API HTTP 404 from the real runner. That result is retained
against the exact repository, draft ID and tag; every later access still checks
the authenticated draft state. Transient access-check errors are retried, never
treated as privacy approval. Never publish these drafts.
Earlier packages remain available. Media and download links stay out of Git.

Each verified render is retained before the next starts. A fresh runner restores
those exact bytes and hash-bound checks, then completes only missing work.
Successful large audio intermediates are removed to fit the free runner's disk.
Prepared archives are smaller than 2 GiB each.

Delivery first restores the receipt. Completed deliveries are checked against
Filebin's live filename/hash/size listing and expiry, then reused. Incomplete
ones resume the same bin, persisting receipts before each external action.
Each file receives one complete download/checksum verification. Retries do not
repeatedly download media and exhaust the service's temporary allowance.
The completed bin is made read-only.

Eight files are delivered: three MP4s, thumbnail, backdrop, two Short covers,
and a text file containing all titles, descriptions, credits and Studio steps.
Filebin is temporary; download before the stated expiry. The private draft
retains the verified archive after its temporary delivery link expires.

## Cloud test or recovery

```bash
gh workflow run daily-production.yml --repo jacktiger91/ambience-video-pipeline \
  -f episode_date=2026-11-06
gh workflow run daily-filebin.yml --repo jacktiger91/ambience-video-pipeline \
  -f mode=preflight -f episode_date=2026-11-06
gh workflow run daily-filebin.yml --repo jacktiger91/ambience-video-pipeline \
  -f mode=delivery-test -f episode_date=2026-11-06
```

Replace the example date with the intended date. Matching ready packages and
deliveries are reused. Never delete a receipt to force another upload. The older
bootstrap modes remain for importing manually curated packages; they do not
perform automatic daily production.

For local development, use Python 3.12, FFmpeg/FFprobe and a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-production.txt
.venv/bin/python -m unittest discover -s tests -p 'test_production.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_scheduled*.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_daily_schedule.py' -v
```

GitHub Actions uses its automatic repository-scoped `GITHUB_TOKEN`. No personal
token or private media URL belongs in source configuration, chat or logs.
Codex's proxy can permit repository API calls while rejecting asset uploads;
native GitHub Actions performs private draft transfers. Test actual repository
operations rather than inferring access failure from `gh auth status`.

## Review

Bookmark [Releases](https://github.com/jacktiger91/ambience-video-pipeline/releases)
while signed in as the repository owner. Open the dated Quiet Hours draft for
its Filebin link and expiry. Public workflow summaries link to this stable
signed-in page without exposing Filebin URLs. Forest Dawn remains in the older
`quiet-hours-private-delivery` draft.

Inspect [Actions](https://github.com/jacktiger91/ambience-video-pipeline/actions)
for failed runs. Read [deployment status](DEPLOYMENT-STATUS.md) for actual cloud
test evidence; deploying a workflow alone is not proof of successful production.
Every 21 days the producer updates a tiny public activity timestamp through the
repository API. This prevents GitHub's 60-day public-repository inactivity rule
from silently disabling the approved schedule. The record contains no media or
download links, and does not claim a package succeeded.

In Studio select `@QuietHoursRelaxingSounds`, upload all three videos and paste
the copy from `upload-details.txt`. Start with Unlisted, choose the playlist,
confirm audience/disclosures and set each Short's Related video and suggested
cover where supported. Check Content for existing uploads and record video IDs
before retrying. Publish the full hour first, then the Shorts. Optional API tools
remain unused unless the user later requests connection.
