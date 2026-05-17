# Session notes — no-code-architects-toolkit

Working doc capturing Claude Code sessions on this repo. Use this to pick up in VS Code without losing context.

**Status as of this update (2026-05-17):** two Docker images built and pushed to Google Artifact Registry. OpenAI Whisper swap implemented end-to-end on a separate branch with fail-fast env validation. Both images smoke-tested locally. Not yet deployed to Cloud Run.

---

## 0. Current artifacts

### Google Artifact Registry

Project: `home-automations-483505` • Region: `us` (multi) • Repo: `no-code-architect-tool-box`

Base URL: `us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit`

| Tag | Branch | Git SHA | Compressed size | Behavior |
|---|---|---|---|---|
| `original-eccbe04` / `original-latest` | docs/session-notes | eccbe04 | 1.70 GB | Local Whisper (`base` model), ffmpeg + Playwright |
| `openai-whisper-0381687` / `openai-latest` | feat/openai-whisper-swap | 0381687 | 1.08 GB | OpenAI Whisper API + fail-fast on missing `OPENAI_API_KEY` |
| `openai-whisper-f4254b6` (orphaned) | feat/openai-whisper-swap | f4254b6 | 1.08 GB | Same as above but no fail-fast — kept as rollback point |

Net savings on OpenAI variant: **~620 MB compressed (36%)**, ~1.86 GB uncompressed (4.82 GB → 2.96 GB).

### Git branches

- **`docs/session-notes`** — original codebase + the minimum changes needed to make it actually build (pip retries, CPU-only torch wheel). Image source for `original-*`.
- **`feat/openai-whisper-swap`** — branched off `docs/session-notes` (commit 731be04). Replaces local Whisper with OpenAI API; drops torch + openai-whisper from deps. Image source for `openai-*`.

Four commits on `feat/openai-whisper-swap` on top of `docs/session-notes` base:
- `3fd4d0e` — swap `services/ass_toolkit.py` + `services/v1/media/media_transcribe.py`; drop `openai-whisper` + `torch` from requirements.txt; remove whisper preload + WHISPER_CACHE_DIR from Dockerfile
- `ebe30ac` — also swap legacy v0 `services/transcription.py` (was missed; would have crashed startup)
- `f4254b6` — add `--retries 10 --timeout 300` to pip installs
- `0381687` — fail-fast validation: `OPENAI_API_KEY` is required at startup

### Smoke test results (both images)

- Gunicorn binds to 8080 within 2s of boot
- 401 returned for missing / wrong `X-API-Key`
- All 34 blueprints register cleanly (proves no broken imports)
- `/v1/toolkit/test` with correct key → 500 ("No cloud storage settings provided") — expected, no S3/GCP env set during smoke test
- For `openai-latest`: confirmed container DIES on boot without `OPENAI_API_KEY`, boots normally with it

---

## 1. What this repo is

Self-hosted Flask API ([app.py](app.py)) bundling **ffmpeg + Whisper + Playwright** in one Docker container to replace paid SaaS (Cloud Convert, JSON2Video, ChatGPT Whisper, PDF.co, Createomate, etc.).

- Routes auto-register from `routes/v1/{category}/{action}.py`
- Each route maps to a service in `services/v1/{category}/{action}.py`
- Three execution modes:
  - **Sync** — no `webhook_url` → blocks
  - **In-process queue** — `webhook_url` provided → returns 202, POSTs result later
  - **GCP Cloud Run Jobs** — `GCP_JOB_NAME` set + webhook → offloads long jobs
- Auth: single `X-API-Key` header against `API_KEY` env var
- Storage abstraction: `services/cloud_storage.py` auto-detects S3 / DO Spaces / GCS from env vars

Job status JSONs land in `LOCAL_STORAGE_PATH/jobs/{job_id}.json`. Status values: `queued | running | done | failed | submitted`.

---

## 2. `/v1/video/caption` deep-dive

Pipeline (in [services/ass_toolkit.py](services/ass_toolkit.py)'s `generate_ass_captions_v1`):

1. **Get timed text:**
   - If caller sent ASS → passthrough
   - If caller sent SRT → parse with `srt`, force style=`classic`
   - Otherwise → **OpenAI Whisper API** (on `feat/openai-whisper-swap`) or `whisper.load_model("base").transcribe(...)` (on `docs/session-notes`)
2. Probe video resolution via `ffmpeg.probe()` → drives `PlayResX/Y` + default font size
3. Resolve position: 3×3 grid + alignment → `\an{1..9}` code + pixel coords
4. Build ASS header (`generate_ass_header` validates font against `matplotlib.font_manager` system fonts; missing font returns 400 with available fonts list)
5. Dispatch to one of five style handlers:
   - `classic` — static
   - `karaoke` — `{\kN}` per word (libass karaoke fill)
   - `highlight` — two-layer trick, current word recolored
   - `underline` — same trick with `{\u1}…{\u0}`
   - `word_by_word` — one word at a time (TikTok-style)
6. Filter `exclude_time_ranges` by overlapping start/end against each `Dialogue:` line
7. Write `.ass` to `LOCAL_STORAGE_PATH/{job_id}.ass`, hand path back to route

Route then runs `ffmpeg.input(video).output(out, vf=f"subtitles='{ass_path}'", acodec='copy')`, uploads, cleans up.

**Known gotchas still present:**
- Video downloaded twice (service + route) — redundant
- SRT input locked to `classic` style
- `/tmp/jobs/` grows forever (see §4a — still open)

---

## 3. OpenAI Whisper API integration (DONE on feat/openai-whisper-swap)

Three service files swapped:

- [services/ass_toolkit.py](services/ass_toolkit.py) — caption pipeline `generate_transcription()`
- [services/v1/media/media_transcribe.py](services/v1/media/media_transcribe.py) — `/v1/media/transcribe` endpoint, supports both transcribe + translate tasks
- [services/transcription.py](services/transcription.py) — legacy v0 `/transcribe-media` endpoint

Shared pattern in each:

1. Extract audio with `ffmpeg -vn -acodec libmp3lame -b:a 64k -ac 1` to a temp `.mp3` (keeps payload under OpenAI's 25 MB limit; ~52 min of audio max).
2. Call `client.audio.transcriptions.create(model="whisper-1", response_format="verbose_json", timestamp_granularities=["word","segment"])`.
3. For `media_transcribe.py` translate task → `client.audio.translations.create(...)` instead. Note: translations endpoint **has no word-level timestamps**; downstream code already handles segment-only.
4. Reshape OpenAI response into the local-Whisper-style dict: `{text, segments: [{start, end, text, words: [{word, start, end}]}]}`. Words are bucketed back into segments by start time.
5. `try/finally` cleans up the temp audio file.

**Caveats:**
- `whisper-1` is the only model with word-level timestamps. `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` are more accurate but no word timing → unusable for karaoke/highlight/word_by_word.
- Cost: ~$0.006/min audio.
- Files >~52 min will fail — chunking not yet implemented.

---

## 4. Cloud Run deployment plan

Repo runs on Cloud Run. Status of the three items previously listed:

### 4a. Job status file cleanup (NOT DONE — still open)

Cloud Run `/tmp` is RAM-backed tmpfs, so `LOCAL_STORAGE_PATH/jobs/` leaks memory. Suggested snippet for [app.py](app.py)'s `create_app()`:

```python
from apscheduler.schedulers.background import BackgroundScheduler
import glob, time

def _cleanup_old_jobs():
    jobs_dir = os.path.join(os.environ.get('LOCAL_STORAGE_PATH', '/tmp'), 'jobs')
    if not os.path.isdir(jobs_dir):
        return
    cutoff = time.time() - 3600  # 1 hour
    for f in glob.glob(os.path.join(jobs_dir, '*.json')):
        if os.path.getmtime(f) < cutoff:
            try: os.remove(f)
            except OSError: pass

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(_cleanup_old_jobs, 'interval', minutes=10)
scheduler.start()
```

APScheduler already in [requirements.txt](requirements.txt). Production-grade fix is moving `log_job_status()` off the filesystem (Firestore / Redis); bigger change.

### 4b. Cost estimate

For a 2 GB / ~60 min 1080p job, Cloud Run `us-central1` late-2025 pricing:

| Item | Estimate |
|---|---|
| Whisper transcription (local `base`, 4 vCPU) | ~30 min wall, ~$0.17 CPU |
| Whisper API (OpenAI variant instead) | $0.36 (60min × $0.006) + ~1 min wall |
| Memory 8 GiB × 1,800s | ~$0.04 |
| ffmpeg burn-in (re-encode 60min 1080p) | ~$0.10 |
| GCS storage of 2 GB result, 1 month | ~$0.04 |
| Egress when user downloads result (2 GB) | ~$0.17 |
| **Per-job total** | ~$0.50 local Whisper / ~$0.71 OpenAI |

OpenAI swap is more expensive on a per-job basis BUT pushes the long-running CPU work off-box, fitting it under the Cloud Run Services 60-min timeout instead of needing Cloud Run Jobs.

### 4c. Image shrink (PARTIALLY DONE)

| Win | Status | Saved |
|---|---|---|
| #4 — drop local Whisper, use OpenAI API | ✅ DONE on feat/openai-whisper-swap | ~620 MB compressed (1.7 → 1.08 GB) |
| #2 — multi-stage Dockerfile | NOT DONE | Estimated additional ~600 MB if applied |
| (#extra) Drop Playwright if `/v1/image/screenshot/webpage` unused | NOT DONE | ~500 MB |

Quick win #4 is live in the `openai-latest` image. Multi-stage Dockerfile rewrite would deliver more savings but needs a clean build verification (high risk of missing a runtime lib in the slim stage).

### 4d. Build resilience fixes (DONE on docs/session-notes)

Two issues blocked the original build that aren't in the original repo:

1. **NVIDIA CUDA wheels**: PyPI's default `torch` for x86_64 manylinux pulls ~2.5 GB of `nvidia-*-cu13` packages (cuDNN, cuBLAS, cuSPARSELt, NCCL, nvshmem, triton, cuda_bindings, cublas). Build failed twice mid-download. Fixed by pre-installing torch from `https://download.pytorch.org/whl/cpu` *before* `openai-whisper` resolves it — torch's CPU wheel has no NVIDIA requirements. Cloud Run has no GPU; CUDA libs never executed at runtime anyway.
2. **Transient pip failures**: added `--retries 10 --timeout 300` to all `pip install` lines.

Committed as `eccbe04` on `docs/session-notes`.

---

## 5. FFmpeg version

Repo pins `n8.1.1` at [Dockerfile:119](Dockerfile:119) — built from source via `git.ffmpeg.org/ffmpeg.git`. Latest 8.1 line release ("Karpov"). Caption endpoint tested and works.

### ASS_FEATURE_WRAP_UNICODE

A **libass** feature, not an ffmpeg flag. The Dockerfile already handles it correctly at [lines 96-114](Dockerfile:96):

1. Compiles `libunibreak` from upstream
2. Compiles `libass` with `./configure --enable-libunibreak`
3. Compiles ffmpeg with `--enable-libass`

Any clean build of this repo gets ASS_FEATURE_WRAP_UNICODE.

---

## 6. Build / run reference

### Build the original image locally

```bash
git checkout docs/session-notes
docker buildx build --platform linux/amd64 -t nca-toolkit:original .
# First build is 15–30 min (compiles ffmpeg + libass from source). Subsequent builds with cache: 1–2 min.
```

### Build the OpenAI-Whisper variant locally

```bash
git checkout feat/openai-whisper-swap
docker buildx build --platform linux/amd64 -t nca-toolkit:openai .
```

### Pull either from Artifact Registry

```bash
gcloud auth configure-docker us-docker.pkg.dev   # one-time
docker pull us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit:original-latest
docker pull us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit:openai-latest
```

### Run with S3 / DO Spaces storage

```bash
docker run -d -p 8080:8080 --name nca \
  -e API_KEY=pick-any-string \
  -e OPENAI_API_KEY=sk-...           # REQUIRED for openai-latest, optional for original-latest \
  -e S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com \
  -e S3_ACCESS_KEY=xxx \
  -e S3_SECRET_KEY=xxx \
  -e S3_BUCKET_NAME=your-bucket \
  -e S3_REGION=nyc3 \
  -e GUNICORN_WORKERS=2 \
  -e GUNICORN_TIMEOUT=300 \
  us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit:openai-latest

curl -X POST http://localhost:8080/v1/toolkit/test -H "X-API-Key: pick-any-string"
```

Apple Silicon: add `--platform linux/amd64` when running (image is amd64).

Local-dev option: `docker compose -f docker-compose.local.minio.n8n.yml up -d` → API on :8080, MinIO console on :9001, n8n on :5678.

### Deploy to Cloud Run (example, not yet executed)

```bash
gcloud run deploy nca-toolkit \
  --image=us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit:openai-latest \
  --region=us-central1 \
  --platform=managed \
  --memory=8Gi --cpu=4 --timeout=3600 \
  --allow-unauthenticated \
  --set-env-vars API_KEY=...,OPENAI_API_KEY=sk-...,S3_ENDPOINT_URL=...,S3_ACCESS_KEY=...,S3_SECRET_KEY=...,S3_BUCKET_NAME=...,S3_REGION=...
```

For long jobs (>60 min), also set `GCP_JOB_NAME` and use Cloud Run Jobs.

---

## 7. Open items / next steps

Resolved this session:
- ✅ Whisper migration scope (all three files swapped)
- ✅ Apply changes (committed to `feat/openai-whisper-swap`)
- ✅ Build images and push to Artifact Registry
- ✅ Smoke tests pass for both
- ✅ Fail-fast validation for `OPENAI_API_KEY`

Still open:
1. **Cloud Run deploy** — not yet executed. Decide region, memory, CPU, env vars, and whether to use Cloud Run Services (60-min cap, but OpenAI swap fits inside) or Cloud Run Jobs (24h cap).
2. **Job-file cleanup snippet** in [app.py](app.py) — §4a above. RAM leak in production otherwise.
3. **Drop Playwright?** Only if `/v1/image/screenshot/webpage` isn't needed. Saves ~500 MB.
4. **Multi-stage Dockerfile rewrite** (quick win #2) — another ~600 MB if pursued.
5. **Audio chunking** for >52-min files on the OpenAI variant (25 MB upload cap at 64 kbps).
6. **Merge `feat/openai-whisper-swap` → `docs/session-notes` (or main)** if ready to make OpenAI the default.

---

## 8. Key file paths for VS Code

- [app.py](app.py) — Flask app, queue, decorators
- [app_utils.py](app_utils.py) — `validate_payload`, `log_job_status`, `discover_and_register_blueprints`
- [config.py](config.py) — env var validation (now includes `OPENAI_API_KEY` fail-fast on feat branch)
- [Dockerfile](Dockerfile) — build (CPU-torch + pip retries applied)
- [requirements.txt](requirements.txt) — Python deps (torch removed on docs branch since it's installed via CPU index; openai-whisper/torch fully gone on feat branch)
- [services/ass_toolkit.py](services/ass_toolkit.py) — caption pipeline (swapped on feat branch)
- [services/v1/media/media_transcribe.py](services/v1/media/media_transcribe.py) — v1 transcribe service (swapped on feat branch)
- [services/transcription.py](services/transcription.py) — legacy v0 transcribe service (swapped on feat branch)
- [services/cloud_storage.py](services/cloud_storage.py) — S3/GCS abstraction
- [services/authentication.py](services/authentication.py) — `X-API-Key` check
- [services/webhook.py](services/webhook.py) — async result POST
- [routes/v1/video/caption_video.py](routes/v1/video/caption_video.py) — caption route
- [routes/transcribe_media.py](routes/transcribe_media.py) — legacy v0 transcribe route (uses services/transcription.py)
- [CLAUDE.md](CLAUDE.md) — architecture context for Claude Code
