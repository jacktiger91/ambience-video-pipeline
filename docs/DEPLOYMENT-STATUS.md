# Deployment status — 5 November 2026 UTC

Automatic daily production is implemented and undergoing its full cloud test.
The schedule will be enabled after source discovery, all three renders, private
recovery and Filebin delivery pass in the actual cloud runner.

Production prepares a day ahead. Delivery attempts now start at 12:00 Cyprus
time, retaining the agreed 18:00 Asia/Nicosia ready-by target. The new dated
private drafts preserve the earlier Forest Dawn package and delivery history.

## Previously verified delivery



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
