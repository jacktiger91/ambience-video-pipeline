# Temporary Filebin delivery

Filebin is a free, public, temporary handoff for approved review media and the
matching titles, descriptions and recording credits. The user explicitly
authorized including this publishing copy with each daily concept on 4 November
2026, with a requested delivery target of 18:00 Asia/Nicosia. It is not
a private secret store or permanent archive. Upload only the files authorized
for the current delivery. Never include OAuth files, tokens, session data, or
private configuration. Keep media and delivery records under ignored `outputs/`.

## Deliver a completed concept

`filebin_delivery.py` builds a fixed upload list from a verified `bundle.json`:
the full hour, two Shorts, thumbnail, two suggested cover images, and a generated
concept-specific `upload-details.txt`. The generic CLI accepts an explicitly
selected backdrop image; include it for the full daily package. The cloud
workflow requires that selection when Filebin delivery is enabled.
It does not upload the bundle manifest, audit documents, private
configuration or other nearby files. The text contains the exact final titles,
descriptions, recording credits and manual Studio settings for that concept.

```bash
python3 filebin_delivery.py --bundle outputs/EPISODE/bundle.json \
  --state outputs/filebin-state/EPISODE.json \
  --backdrop media/CONCEPT/scene.png --check-local-only

python3 filebin_delivery.py --bundle outputs/EPISODE/bundle.json \
  --state outputs/filebin-state/EPISODE.json \
  --backdrop media/CONCEPT/scene.png --approved-filebin
```

Keep the same state path on retry. `--approved-filebin` records the existing
user authorization; it does not make unrelated files eligible for transfer.
The command reuses matching uploads, completes only missing checks, and stops
if a local file or its final copy changed. Expired or restricted delivery links
need a reviewed replacement; the command does not silently create new bins.

The private daily workflow in `.github/workflows/daily-filebin.yml` restores
receipts from the dedicated `quiet-hours-private-delivery` **draft release**.
It saves immutable numbered receipt assets before further uploads, and writes
the completed Filebin link into that draft's review notes. The runner verifies
`draft: true` on each release access and fails if the release is published.
Anonymous draft access must be checked from an actual GitHub runner before
automatic delivery is enabled. The public run summary reports status and links
to the signed-in review page, without exposing Filebin URLs. An already
completed matching delivery is reused, so a retry does not consume another
full-media download or create another bin.

The older optional `ambience-review.yml` route first restores a receipt
using `filebin_cloud_state.py`, then synchronizes every state change before the
next transfer. The initial bin identity is persisted before the first Filebin
request. The dedicated `ambience-filebin-state` branch contains only allowlisted
filenames, hashes, sizes, Filebin URLs, expiry and verification flags. In a public
repository these receipt links are public too. No media, description text,
source paths, credentials or signed storage URLs enter that branch. A failed
sync stops the delivery. This is separate from the stricter YouTube ID ledger.
That public branch-ledger option is off by default and is not used by the
private scheduled route.

## Verification without exhausting downloads

Use the normal upload API, then **one complete download per file**:

1. Calculate each local file's size and SHA-256 before upload. Finish local media
   decoding and playback checks first; uploading does not replace those checks.
2. POST the complete body with `Content-Length` and `Content-SHA256`. Compare the
   server's returned size and SHA-256 with the local values. Record each accepted
   file, stable public URL, and bin expiry immediately.
3. If an upload response is lost, reconcile the bin's JSON listing before retrying
   the upload. A new empty bin can return `files: null`; treat that as an empty
   list. An existing matching size/hash is sufficient to recover the upload step.
4. Follow the normal verification-cookie and HTTPS redirect flow for one full
   GET. Stream the response into a SHA-256 calculation, count every byte, and
   compare both with the local file. Use a bounded timeout and report progress.
   Discard downloaded bytes when another local copy is unnecessary. Do not log
   cookies or the temporary signed storage URL.
5. Record completed checks so a retry does not download verified files again.
   Keep server hash verification, complete download verification, and local
   playback verification as separate facts. A partial download is not a full
   download check.
6. After all intended files pass, make the bin read-only using the documented
   API. Share the stable Filebin URLs and returned expiry, not signed redirects.
   Read-only status does not make the files private or permanent.

Do not split a file into many range requests through Filebin for verification.
Do not run repeated download benchmarks on delivery links. Each accepted file
request consumes a download allowance before the storage redirect, even if the
client requests only a range or subsequently abandons the transfer.

## Download-limit incident and recovery records

During Quiet Shore delivery on 2026-11-04, parallel range verification consumed
the long video's download allowance. Filebin then returned HTTP 403 with:

```text
The file has been requested too many times.
```

This response is different from a temporary signed-link expiry or network
failure. Stop repeated requests when it appears. The six smaller media files
completed full downloads with matching SHA-256 values; that did not establish
that the blocked long-video link remained usable.

Preserve the previous delivery record and its failed or partial verification
evidence when preparing an authorized replacement delivery. Use a separate
record for the replacement, and retain the relationship between attempts.
Do not silently mark a blocked link as delivered, repeatedly recreate bins, or
use an incomplete archive as a substitute for the requested files.

## Provider behavior checked on 2026-11-04

The live site's About page reported `v1.4.0-1-gb384033`. These details are
implementation observations from that version, not promises of future limits:

- The download count belongs to a file reference identified by bin and filename,
  rather than to the globally deduplicated SHA-256 content. New file references
  start at zero; overwriting an existing filename preserves its count. No timed
  reset is present in the inspected counter logic. Do not assume waiting or
  overwriting will restore a blocked link.
- A normal POST receives and hashes the complete request body. If that SHA-256
  already exists in storage, Filebin can skip its own backend storage upload.
  This does not eliminate the client's transfer of the full request body.
- The documented ZIP route is `/archive/<bin>/zip`. The inspected implementation
  filters out files that have reached their download limit before constructing
  the archive. A successful ZIP response can therefore omit a blocked file;
  archive availability alone does not establish a complete delivery.
- Filebin settings and service availability can change. Read expiry from the
  actual API response and avoid promising permanent access or a fixed number
  of downloads.

References:

- [Filebin API](https://filebin.net/api.yaml)
- [Deployed file download and upload implementation](https://github.com/espebra/filebin2/blob/b384033/internal/web/http_file.go)
- [Deployed file counters and persistence](https://github.com/espebra/filebin2/blob/b384033/internal/dbl/file.go)
- [Deployed archive implementation](https://github.com/espebra/filebin2/blob/b384033/internal/web/http_bin.go)
- [Deployed database schema](https://github.com/espebra/filebin2/blob/b384033/internal/dbl/schema.sql)
