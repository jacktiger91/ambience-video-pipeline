# Deployment status — 5 November 2026 UTC

Automatic daily production and Filebin delivery are enabled on `main` after
successful end-to-end cloud tests. Standard free GitHub Ubuntu runners do the
work with no laptop, paid API or Google Cloud account. YouTube upload and
publication remain manual.

The target is **18:00 Asia/Nicosia**. Production prepares a day ahead, with
three daily attempts; Filebin delivery starts at noon and retries before the
deadline. Timezone-aware selection handles daylight-saving changes. The target
is best effort because GitHub scheduling and Filebin have no timing SLA.

**6 November — Quiet Bay is complete and delivered.** It contains a one-hour
1920 × 1080 H.264/stereo AAC video, two distinct 25-second 1080 × 1920 Shorts,
matching thumbnail and backdrop, suggested cover frames, and finished titles,
descriptions and actual recording credits. The natural soundtrack uses eight
nonoverlapping passages from five CC BY field recordings. The moving fixed
camera scene is separately licensed real footage, explicitly credited.

All three exports fully decoded. The full-hour loudness measured −22.1 LUFS
with −4.1 dBTP; the Shorts measured −21.6/−23.5 LUFS with safe peak headroom.
Fixed-camera and subject-motion checks passed. These are automated checks,
not a fabricated listening review. The actual thumbnail and both cover frames
were also visually inspected.

Verified cloud runs:

- [Source discovery through all three finished renders and private storage](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/42080947920)
- [Complete package restoration on a fresh runner](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/42084006185)
- [Eight-file Filebin upload, full download/hash checks and read-only handoff](https://github.com/jacktiger91/ambience-video-pipeline/actions/runs/42084303875)

The affected local checks passed 52 tests, including a simulated interrupted
render restored into a fresh workspace without recreating its verified hour.
The real footage loop also passed a short FFmpeg render/decode check.

[Private daily downloads and retained packages](https://github.com/jacktiger91/ambience-video-pipeline/releases)
are available while signed in as the repository owner. Open the dated Quiet
Hours draft. Never publish these drafts. Filebin links expire; their exact
expiry appears with each completed handoff. Earlier drafts remain available.

The cloud activity timestamp update also succeeded. It repeats at most once
per 21 days to prevent GitHub's 60-day inactivity rule from disabling the
approved schedule. No download links or media enter the public repository.

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
