# Postman test suite — NCA Toolkit API

A working Postman collection covering the v1 endpoints. Use it to smoke-test a new deployment, verify auth, and sanity-check the media-processing path end-to-end.

## Files

- `nca-toolkit.postman_collection.json` — 49 requests grouped into 12 folders. Each request has assertion scripts that check status, response shape, and performance. Most include a `description` documenting the payload + the v1 successor for legacy routes.
- `nca-toolkit.postman_environment.json` — variables (`baseUrl`, `apiKey`, sample media URLs, optional `webhookUrl`, optional `gcpBucketName`, optional `gdriveFolderId`, `sampleSrt`). Override with your live values.

## Import

In Postman:
1. **Collections** → Import → drop in `nca-toolkit.postman_collection.json`.
2. **Environments** → Import → drop in `nca-toolkit.postman_environment.json`.
3. Select the imported environment from the top-right dropdown.

## Set your values

Open the environment and replace these:

| Variable | Example | Notes |
|---|---|---|
| `baseUrl` | `https://nca-toolkit-108769234292.europe-west1.run.app` | No trailing slash. |
| `apiKey` | `your-API-key` | The `API_KEY` env var on the running service. |
| `sampleVideoUrl` | (defaulted to a 1 MB Big Buck Bunny clip) | Any publicly fetchable mp4. |
| `sampleAudioUrl` | (defaulted to a short public mp3) | Any publicly fetchable audio. |
| `sampleImageUrl` | (defaulted to a small public jpg) | Any publicly fetchable image. |
| `webhookUrl` | `https://webhook.site/abc-123` | Optional. Used by the async-pattern folder and any `webhook_url` variant in folders 11–12. Grab a free URL at https://webhook.site. |
| `gcpBucketName` | `olah-tv-test-bucket` | Only used by `/v1/gcp/upload`. Must be the bucket the runtime SA has write access on. |
| `gdriveFolderId` | (no default) | Required for `/gdrive-upload`. ID from the folder URL `drive.google.com/drive/folders/<this>`. **Share the folder with the runtime SA email** as Editor or the upload 500s. |
| `sampleSrt` | (defaulted to a 2-line stub) | Used by `/caption-video` (inline-SRT variant). Override if you want to test your own subtitles. |

## Run

**Single request:** click → Send.

**Whole folder or collection:** Runner (▶︎ icon) → select Collection → Run. Sequential is fine; concurrent will saturate the single-vCPU paths on local-Whisper builds.

**From the CLI:** `newman run nca-toolkit.postman_collection.json -e nca-toolkit.postman_environment.json`

## Folder map

| # | Folder | What it covers | Cost / time |
|---|---|---|---|
| 1 | Auth & Health | Missing key → 401, bad key → 401, good key → 200. Confirms the deployment is reachable + the API key works. | <1s |
| 2 | Job Status | Polling shape for `/v1/toolkit/job/status`. | <1s |
| 3 | Media — info | `/metadata`, `/silence`, `/transcribe`. Transcribe is **slow on the local-Whisper image** (CPU-bound, ~real-time). | 1s–60s |
| 4 | Media — convert | `/convert/mp3`, `/convert`, `/BETA/media/download`. | 2–10s |
| 5 | Video | `/thumbnail`, `/trim`, `/cut`, `/split`, `/concatenate`, `/caption`. Caption invokes Whisper. | 2–60s |
| 6 | Audio | `/audio/concatenate`. | 2–10s |
| 7 | Image | `/image/convert/video`. | 2–10s |
| 8 | Cloud upload | `/gcp/upload`. Tests Secret-Manager-wired SA credentials. | 2–5s |
| 9 | FFmpeg + Code | `/ffmpeg/compose`, `/code/execute/python`. | 1–5s |
| 10 | Async pattern | Same `/metadata` call but with `webhook_url` set — expects 202 immediately and a POST to your webhook URL when done. | <1s + webhook delivery |
| 11 | Legacy v0 routes | Pre-v1 endpoints kept for back-compat: `/audio-mixing`, `/authenticate`, `/caption-video` (SRT and ASS variants), `/combine-videos`, `/extract-keyframes` (sync + webhook), `/gdrive-upload` (basic + full), `/image-to-video` (basic + zoom), `/media-to-mp3` (basic + bitrate), `/transcribe-media` (transcript/srt/ass output formats). Each request's `description` notes its v1 successor. **`/gdrive-upload` needs `gdriveFolderId` set + folder shared with the runtime SA.** | 1s–60s |
| 12 | Additional v1 routes | `/v1/media/generate/ass` — full ASS generator with three variants: defaults, TikTok-style (vertical + highlight), and a clean-up variant with `replace` + `exclude_time_ranges`. Plus `/v1/s3/upload` — public, private with `download_headers`, and async. **S3 endpoints require S3_* env vars on the running service — they 500 on a GCS-only deploy.** | 2–60s |

## What the assertions check

Every request runs Postman test scripts that assert at minimum:

- **Status code** matches expectation (200, 202, 400, 401, 404).
- **Response time** under a sensible cap (2s for cheap calls, 120s for transcription).
- **Response shape** for 200/202: presence of `code`, `job_id`, `response`, `endpoint`, `run_time` fields per the toolkit's standard envelope ([app_utils.py](../../app_utils.py) `queue_task_wrapper`).
- **Auth-failure shape** for 401: presence of error message.

Failures show up in the Tests tab — Postman highlights the line.

## Pitfalls

- **`/v1/video/caption` on the local-Whisper image takes ~real-time.** A 10s clip takes ~10–30s wall-time. Don't conclude the deploy is broken if it doesn't return in 2s. The OpenAI variant returns in ~1–3s instead.
- **Cloud Run timeout = 300s currently.** Anything longer 504s. Long transcriptions need `--timeout=3600` or the OpenAI variant. See [SESSION-NOTES.md](../../SESSION-NOTES.md) §8 item 4.
- **`/v1/gcp/upload` will fail if `GCP_SA_CREDENTIALS` isn't wired.** This is the canonical test that Secret Manager wiring is working — a 200 here means the secret is loading + the SA has bucket write.
- **`webhook_url` tests need a public webhook receiver.** Use https://webhook.site or RequestBin. Cloud Run can't reach `localhost`.

## Updating

When you add a new route in `routes/v1/{category}/{action}.py`, add a matching request in the right folder. Keep the pre-request script + test script pattern consistent — copy from an existing one. The conventions:

- URL: `{{baseUrl}}/v1/...`
- Header: `X-API-Key: {{apiKey}}`
- Body: raw JSON with `{{sampleVideoUrl}}` etc. for any media inputs.
