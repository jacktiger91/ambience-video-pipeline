# Revised Night Rain validation

The revised media is finished in `outputs/revised-night-rain/review/`, under
bundle ID `revised-night-rain-veranda-v2`. Rendering and checks ran in Linux
Cloud. No paid external service was used. Earlier drafts remain in
`outputs/first-test/`.

| Export | Resolution | Duration | Encoded AAC loudness | True peak |
|---|---|---|---|---|
| Full hour | 1920×1080 | 3600.000s | −23.1 LUFS | −2.5 dBTP |
| Short 1 | 1080×1920 | 25.000s | −22.8 LUFS | −3.7 dBTP |
| Short 2 | 1080×1920 | 25.000s | −19.1 LUFS | −3.7 dBTP |

All three contain H.264 video and stereo 48 kHz AAC. Every video frame and audio
packet decoded successfully through the expected endpoint. The long export is
1,411,478,173 bytes. Its SHA-256 is
`71326cde33cfb7ce06d421872f8dc07c59568279c1bde15fd421e83e1ecc567a`.
The bundle and portable `verification.json` record all file hashes and checks.

## Corrections and evidence

- Replaced synthesized rain with four documented stereo field recordings, each
  independently verified under CC0. Unique passages are joined with 16-second
  crossfades; no audio passage is looped. The opening emphasizes close drops.
- Corrected possible clipping in intermediate audio by retaining floating-point
  PCM before peak limiting. Final processing uses measured constant gain and
  an oversampled lookahead limiter, preserving natural level variation without
  automatic gain riding. Failed intermediate diagnostics are retained.
- Rebuilt the scene as photographic-style AI artwork, with fixed camera,
  sheltered rain masks, three rain depths/speeds, matched incoming drops and
  impacts, perspective ripples, and five subtle thunder glows. AI visuals are
  disclosed in the prepared metadata.
- Strengthened full-decode evidence to verify the actual endpoint. Bundle reuse
  preserves the decoded endpoint and checks the exact source SHA.
- Added supplied-thumbnail normalization, fractional portrait crops,
  source-specific Short credits and a brief channel invitation. Both cover
  frames and the 1280×720 thumbnail were visually checked.

Six mixer tests, six animation tests and ten bundle tests passed. Affected
follow-up regressions passed after their changes. An offline uploader preflight
validated metadata and file hashes. Repeating bundle preparation reused all six
media assets unchanged.

Automated audio checks found no dropout or nonfinite samples in the three
crossfade windows, verified distinct left/right signals, and measured the final
AAC levels above. Source-wide event classification supported rain/drops and
the selected thunder cues, with no speech/music score at or above 0.25.
These are technical and model-based checks; no subjective listening was
possible. See [audio QA evidence](provenance/revised-night-rain/AUDIO-QA.md) and
[recording permissions](provenance/revised-night-rain/CREDITS.md).

## YouTube remains blocked

The runtime connection check failed because the protected OAuth client file is
unavailable; no usable client/refresh grant is configured here. No authenticated
channel ID, live refresh, new upload ID, Studio edit link or actual visibility
has been verified. No YouTube upload was attempted. Prepared metadata requests
Unlisted for all three.

Attach the existing uploader's authorization through the protected secret store
described in [YouTube setup](YOUTUBE.md), then run the channel/refresh check and
stage this existing bundle. The resumable uploader records each successful ID
and reconciles retries. Never paste credentials into chat, Git or review files.
Manual Studio uploads of the finished MP4s are also possible without API-project
registration.

The playlist selection is pending. Shorts Related video and cover selection
remain manual where YouTube offers them. No public publishing or schedule was
enabled; the start time still requires confirmation in Asia/Nicosia.
