# Deployment status — 4 November 2026 UTC

The private Filebin delivery workflow is deployed on `main` and enabled after
its cloud tests passed. It uses standard GitHub-hosted Ubuntu runners in this
public repository, without paid services or a running personal computer.
Delivery attempts start at 16:00 Asia/Nicosia for a ready-by target of 18:00.
The first timed window is 5 November; that timed invocation has not occurred
yet. Manual tests exercised the same delivery logic in fresh cloud runners.

**Prepared coverage: 5 November 2026, Forest Dawn, only.** Its full-hour video,
two distinct 25-second Shorts, thumbnail, backdrop, cover frames and finished
titles/descriptions/credits are already delivered and download-verified.
The assistant still needs to prepare the following daily concepts. The
scheduler delivers prepared packages; it does not research recordings or
generate an indefinite supply of new scenes and audio.

Open the [private review draft from Releases](https://github.com/jacktiger91/ambience-video-pipeline/releases)
while signed into the repository owner's account. Keep this release as a draft.
It retains the Filebin review link, complete prepared archive and immutable
delivery receipts. YouTube uploads and publication remain manual.

Verified cloud runs:

- [Private access and small source import](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/41921108084)
- [Filebin upload and complete download](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/41921613212)
- [Full package import and private archive verification](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/41924004263)
- [Fresh-runner restoration of the complete archive](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/41925006465)
- [Live availability check and reuse of the existing delivery](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/41925331138)

All three MP4s fully decoded in Codex Cloud; exported stream specifications,
motion reviews and measured audio levels are recorded in
[Forest Dawn's final QA report](provenance/forest-dawn/FINAL-QA.json).
The renderer smoke test passed all nine cases. Existing core checks passed
91 tests; queue/schedule checks passed 14, and runner/bootstrap checks passed 16.

The deployment corrected draft-tag handling, unstable draft-specific bookmarks,
and readiness reports that previously trusted an old receipt without checking
the current hosted files. Retries check exact file identities and preserve the
existing upload. Filebin's delayed response during the large transfer was
reconciled against its server listing; the accepted MP4 was reused.
