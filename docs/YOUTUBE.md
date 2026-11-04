# Connect YouTube once, then run from the cloud

The renderer and uploader use Python's standard library, FFmpeg, FFprobe, and
YouTube's free API quota. Do not enable billing, purchase credits, or enable a
paid runner for this workflow. Credentials have not been supplied to this
workspace, so a live channel connection, refresh, and upload remain unverified.

The intended handle is **@QuietHoursRelaxingSounds**. The code resolves that
handle independently through `channels.list(forHandle=...)`, then compares its
channel ID with `channels.list(mine=true)` for the signed-in account. A mismatch
stops the operation before any upload. Never substitute the signed-in channel's
ID as the intended ID merely to make this check pass.

## 1. Configure a free Google Cloud project

1. Open <https://console.cloud.google.com/> and use the project selector to
   select a project you control or create a dedicated project such as
   `Quiet Hours YouTube`. YouTube Data API v3 does not require attaching a
   billing account for its free quota.
2. In **APIs & Services → Library**, find **YouTube Data API v3** and select
   **Enable**. Keep this project selected for the following steps.
3. Open **Google Auth Platform** (older layouts call this **OAuth consent
   screen**). Under **Branding**, enter the app name, support email, and developer
   contact email. Use your own details; this tool does not invent an identity.
4. Under **Audience**, use **External** for a personal Google/YouTube account.
   Internal is only for an eligible Google Workspace organization. If the app
   remains in **Testing**, add the Google account that owns or manages the
   intended YouTube channel as a test user.
5. Under **Data Access**, begin with
   `https://www.googleapis.com/auth/youtube.upload`. The base authorization
   command requests only this scope. The requested workflow also needs
   `https://www.googleapis.com/auth/youtube.readonly` to verify the authenticated
   channel, recover prior uploads, and inspect actual visibility. Add it for the
   recommended `--verify-channel` command below. These reads cannot safely be
   replaced by assuming the channel or upload visibility.
6. Only if you want the tool to modify a playlist automatically, add
   `https://www.googleapis.com/auth/youtube.force-ssl` and use
   `--manage-playlist`. Otherwise keep that broader scope absent and apply the
   playlist in Studio. Setting a custom thumbnail uses the upload grant.
7. Under **Clients → Create client**, select **Desktop app**. Give the client a
   descriptive name and download its JSON. Do not create a Web application
   client or configure an out-of-band redirect. The command uses a temporary
   `127.0.0.1` loopback listener with state validation and PKCE.

For daily use, move the OAuth audience/publishing status from **Testing** to
**In production**, following any verification requirements Google displays.
Testing-mode refresh tokens for these YouTube scopes normally expire after
**seven days**. Changing the app status does not prove an old testing token has
become durable: authorize again after the change, then test refresh from the
cloud. Production grants can still be revoked, invalidated, or expire through
inactivity; the job must report failed refreshes.

OAuth publishing status and YouTube API project compliance are separate. API
projects subject to YouTube's audit restriction can have uploads forced to
**private**, even when the request explicitly says **unlisted**. Moving OAuth to
Production does not remove that restriction; follow the YouTube API compliance
audit process or upload the supplied MP4 manually through Studio.

Official references:

- [YouTube OAuth for installed applications](https://developers.google.com/youtube/v3/guides/auth/installed-apps)
- [OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Upload visibility and unverified-project restriction](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Channels lookup](https://developers.google.com/youtube/v3/docs/channels/list)

## 2. Store local credentials privately and authorize

Run this one-time command on a computer with Python 3.10+ and a browser. The
computer may be your laptop; subsequent refreshes, rendering, and uploads run in
the cloud while that laptop is off. Do not run the browser sign-in in a headless
scheduled job or paste authorization codes, downloaded JSON, or tokens into chat.

On Linux/macOS, place the downloaded JSON here, **outside the repository**:

```text
~/.config/ambience-youtube/client_secret.json
```

Prepare the directory and permissions:

```bash
mkdir -p "$HOME/.config/ambience-youtube"
chmod 700 "$HOME/.config/ambience-youtube"
# Move the downloaded Desktop client JSON into the path above using your file manager.
chmod 600 "$HOME/.config/ambience-youtube/client_secret.json"
```

Windows users can use the same Python script from a local checkout, with explicit
`--client-secret` and `--token-file` paths in a private directory under their user
profile. Use Windows file permissions to restrict the directory to that user.
The cloud job itself remains Linux-based and has no Windows dependency.

For the requested staging workflow, the required channel/retry/visibility checks
justify adding the read-only scope at this first sign-in:

```bash
python3 youtube_auth.py authorize \
  --client-secret "$HOME/.config/ambience-youtube/client_secret.json" \
  --token-file "$HOME/.config/ambience-youtube/token.json" \
  --expected-handle '@QuietHoursRelaxingSounds' \
  --oauth-status production \
  --verify-channel
```

Use `--oauth-status testing` instead if that is the actual console setting.
The command records your declared status; it cannot inspect the consent-screen
setting for you. If you only want the initial upload-only grant, omit
`--verify-channel`. Such a grant cannot pass staging's required identity and
recovery checks; add the scope before staging. Add `--manage-playlist` only when
automated playlist modification is required.

Google's consent page opens automatically. Select the account and, where Google
offers it, the correct YouTube/Brand Account. Grant offline access. The browser
returns to the temporary local loopback page. The command resolves the intended
handle, checks the channel ID, and writes only the refresh grant and non-secret
metadata to `~/.config/ambience-youtube/token.json` with mode `0600`. It does not
print the token or authorization code. A failed channel check does not save a
new token. Existing token files are replaced only after successful authorization.

## 3. Put the two JSON documents into the scheduled job's secret store

In the GitHub repository, open **Settings → Environments**, create or select the
environment **`youtube-review`**, and use **Environment secrets → Add secret**:

| Environment secret name | Secret value, entered securely in GitHub |
| --- | --- |
| `YOUTUBE_OAUTH_CLIENT_JSON` | Entire downloaded Desktop-client JSON file |
| `YOUTUBE_OAUTH_TOKEN_JSON` | Entire protected `token.json` generated by the command |

Use the protected secret editor, not an issue, chat, commit, shell command-line
argument, workflow log, or plaintext artifact. GitHub stores the values encrypted
and makes them available only to jobs referencing that environment. Add required
reviewers and deployment-branch restrictions where your repository plan supports
them. Never expose these secrets to untrusted pull-request code.

The scheduled job must declare `environment: youtube-review` and inject the
secrets into those two environment variable names. The tools read them directly
without writing credential files. If you use another trusted cloud runner, use
its encrypted secret store and inject the same names. A proxy-only placeholder
is not a raw refresh token: OAuth refresh needs the real JSON in the trusted
runner process. Do not copy a proxy placeholder into token JSON.

For local/cloud file-based operation, the paths above remain supported. Files
must have owner-only permissions on Linux. The client and token files never
belong in Git or a downloadable review bundle.

## 4. Verify refresh from the actual cloud runner

Once the two environment secrets are injected, run:

```bash
python3 youtube_auth.py check --expected-handle '@QuietHoursRelaxingSounds'
```

This exchanges the refresh token for a fresh access token, resolves the intended
handle independently, and checks the authenticated channel ID. It prints the
verified ID, never credentials. It exits nonzero for Testing/unknown status so
an expiring grant cannot be called ready for daily use. A manual first test can
use `--allow-testing`; that prints the seven-day warning and explicitly remains
unsuitable for unattended daily operation.

The cloud needs HTTPS access to `oauth2.googleapis.com` and
`www.googleapis.com`. Local browser sign-in also uses `accounts.google.com`.
Maintain TLS verification and existing proxy settings. Add missing destinations
through environment settings; do not bypass a denied network policy.

The daily schedule stays disabled until all three test videos are approved and
you confirm a start time in **Asia/Nicosia**. A single successful token refresh
does not by itself enable any schedule or publish any video.

## Staging and manual Studio settings

The current test should leave the channel's older upload untouched. Stage the
new three-video bundle only after its media and upload metadata are ready:

```bash
python3 youtube.py stage \
  --bundle outputs/test/bundle.json \
  --journal "$HOME/.local/state/ambience-youtube/test.private.json" \
  --expected-handle '@QuietHoursRelaxingSounds' \
  --review-report outputs/test/youtube-review.json
```

The private journal stores resumable-session information and must remain in a
protected store outside Git and downloadable artifacts. Keep it across retries.
The tool writes successful YouTube IDs immediately. For an ephemeral cloud
runner, also supply `--ledger PATH --ledger-sync-command EXECUTABLE` using the
workflow's durable ledger helper; the ledger contains non-secret attempt/ID
state, while private resumable URLs remain excluded from review artifacts.
An interrupted/ambiguous upload must be reconciled before creating another one.

Only use `--existing KEY=VIDEO_ID` when that particular existing upload has been
explicitly approved for reuse as `long`, `short1`, or `short2`. Never match uploads
by title alone. Add `--apply-playlist` only when the bundle contains the approved
playlist ID and the grant includes `youtube.force-ssl`; otherwise apply the
playlist manually in Studio. Stage permits a manual test with a Testing grant
but reports its expiry warning; it does not declare the connection daily-ready.

Staging explicitly requests **unlisted**, then reads YouTube's actual returned
status. Inspect the generated review report and use the recorded video IDs to
resume; never delete the upload journal to force a retry. A Studio edit link is
`https://studio.youtube.com/video/VIDEO_ID/edit`. If YouTube forces an upload to
private, inspect the restriction in Studio; changing OAuth mode alone will not
lift a project audit restriction. If Studio permits a manual visibility change,
review it there; otherwise use the supplied MP4 for a manual Studio upload or
complete YouTube's audit.

The ordinary YouTube Data API does not expose the Shorts **Related video**
selection. Set each Short's Related video to the full upload in Studio when that
channel feature is available. Suggested cover frames are supplied as images;
the YouTube app/Studio controls which Shorts cover-selection tools are available.
Review audience classification, altered/synthetic-content disclosure, credits,
playlist, and thumbnail before publishing. Publish the full video first, then
the Shorts. The tools do not set a public status or publication time.
