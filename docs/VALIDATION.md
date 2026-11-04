# First test: validation and remaining work

The requested earlier audit report and retained production media were not present
in this workspace. After clarification, the user requested a fresh original
test and then chose to skip Google Cloud/API registration. The existing channel
upload was not changed. No billing, paid service, publication, or schedule was
enabled.

## Completed in the current Codex cloud machine

- Created an original Quiet Night Rain illustration and 3,600 seconds of
  continuous synthesized stereo ambience; no third-party recordings or music.
- Produced `outputs/first-test/review/long.mp4`: H.264/yuv420p, 1920×1080,
  stereo AAC/48 kHz. Audio is 3,600 seconds; container/video is 3,600.066667
  seconds, within the documented frame rounding tolerance.
- Produced two distinct 20-second 1080×1920 Shorts from offsets 300 and 2,100
  seconds with left/right framing and a brief invitation to the full hour.
- All three passed full video/audio decode checks. The bundle reused the
  original's SHA-matched full-decode evidence instead of decoding it again.
- Produced a 1280×720 thumbnail, two cover suggestions, accurate credits and
  synthetic-content disclosures, finished titles/descriptions, manual Studio
  instructions, a portable preview page, and a verified 190.6 MiB ZIP package.
- Repeated bundle preparation reused all media unchanged. Real media passed
  the uploader's offline preflight without any authentication or upload.

## Review fixes

- Recovered/uploaded video IDs now carry `attempt_started: true`, matching the
  durable ledger schema; previously a recovered ID could fail cloud-state sync.
- Private resumable-session journals must remain outside the checkout with
  owner-only access. Journal, lock, report and ledger path aliases/collisions
  are rejected before writes. Common OAuth filenames are also ignored by Git.
- Upload recovery matches a stable job marker plus the media SHA-256, stops on
  ambiguous prior attempts, and persists nonsecret intent before initialization
  and each returned ID before follow-up operations. Sensitive session URLs stay
  out of the durable Git ledger, review files and logs.
- Full-decode evidence can be reused only for matching media hashes and expected
  duration, including the source's valid frame-rounding tolerance.

## Checks

56 automated checks passed across the affected suites: 9 renderer smoke tests,
7 bundle tests, 4 cloud-ledger tests, 5 daily-planning/guard tests, 2 original
creation tests, 20 uploader contract tests, and 9 OAuth tests. The final uploader
changes were checked by rerunning its affected 20-test suite. OAuth and upload
tests use simulated responses; they are not evidence of a live channel connection.
Workflow YAML and Bash step syntax were checked, but no GitHub-hosted workflow
was executed. No generated media or credential files are tracked in Git.

## Not completed or enabled

- No real Google OAuth authorization, cloud token refresh, authenticated channel
  ID verification, or YouTube upload. There are no new YouTube IDs or Studio edit
  links. The environment also currently denies the required YouTube/Google
  network destinations; optional domain additions were saved only as a draft.
- Manual Studio upload requires choosing Unlisted for each video, selecting the
  desired playlist, reviewing disclosures, and setting each Short's Related
  video and cover where supported. Publish the long video first after review.
- The manual GitHub workflow is prepared but not deployed. It refuses private
  repositories by default for the selected free-runner route and requires
  confirmation that paid Actions/storage usage is blocked. Optional API staging
  and retained review artifacts are off by default.
- The daily schedule is disabled. Activation still requires approval of the
  complete test and a start time in Asia/Nicosia. Optional API automation needs
  its separate free Google developer registration and a tested OAuth grant;
  manual Studio upload needs neither.
