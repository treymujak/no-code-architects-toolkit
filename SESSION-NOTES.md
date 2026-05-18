# Session notes — no-code-architects-toolkit

Working doc capturing Claude Code sessions on this repo. Use this to pick up in VS Code without losing context.

**Status as of this update (2026-05-18):** three Docker images built. OpenAI Whisper swap done. CVE remediation done — multi-stage rewrite, Playwright dropped, gnutls removed, base packages upgraded. Original 21-CVE list: 20 closed, 1 unfixable upstream. Final image (`secured-v4`) has 6 remaining HIGH findings, all upstream-unfixed and assessed as unreachable in this app. **Live on Cloud Run:** `nca-toolkit` in europe-west1, running `secured-925622a`, GCS SA credential via Secret Manager. OpenAI variant not yet deployed.

---

## 0. Current artifacts

### Google Artifact Registry

Project: `home-automations-483505` • Region: `us` (multi) • Repo: `no-code-architect-tool-box`

Base URL: `us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit`

| Tag | Branch | Git SHA | Compressed size | Behavior |
|---|---|---|---|---|
| `original-eccbe04` / `original-latest` | docs/session-notes | eccbe04 | 1.70 GB | Local Whisper (`base` model), ffmpeg + Playwright + gnutls |
| `openai-whisper-0381687` / `openai-latest` | feat/openai-whisper-swap | 0381687 | 1.08 GB | OpenAI Whisper API + fail-fast on missing `OPENAI_API_KEY` |
| `openai-whisper-f4254b6` (orphaned) | feat/openai-whisper-swap | f4254b6 | 1.08 GB | Same as above but no fail-fast — kept as rollback point |
| `secured-925622a` / `secured-latest` | docs/session-notes | 925622a | TBD (3.08 GB unc) | Multi-stage local-Whisper, no Playwright, no gnutls, no python3.13. **Shipping candidate for nca-local.** |
| `secured-openai-3ed5951` / `secured-openai-latest` | feat/openai-whisper-swap | 3ed5951 | TBD (1.22 GB unc) | Multi-stage OpenAI, no Playwright, no gnutls, no python3.13, no torch. **Shipping candidate for nca-openai.** |

Sizes uncompressed:
- Original local-Whisper: 4.82 GB → `secured-925622a`: **3.08 GB** (-36%)
- Original OpenAI: 2.96 GB → `secured-openai-3ed5951`: **1.22 GB** (-59%)
- OpenAI multistage vs original-original: **-75%**

### Git branches

- **`docs/session-notes`** — original codebase + the minimum changes needed to make it actually build (pip retries, CPU-only torch wheel). Image source for `original-*`.
- **`feat/openai-whisper-swap`** — branched off `docs/session-notes` (commit 731be04). Replaces local Whisper with OpenAI API; drops torch + openai-whisper from deps. Image source for `openai-*`.

Five commits on `feat/openai-whisper-swap` on top of `docs/session-notes` base:
- `3fd4d0e` — swap `services/ass_toolkit.py` + `services/v1/media/media_transcribe.py`; drop `openai-whisper` + `torch` from requirements.txt; remove whisper preload + WHISPER_CACHE_DIR from Dockerfile
- `ebe30ac` — also swap legacy v0 `services/transcription.py` (was missed; would have crashed startup)
- `f4254b6` — add `--retries 10 --timeout 300` to pip installs
- `0381687` — fail-fast validation: `OPENAI_API_KEY` is required at startup
- `3ed5951` — cherry-pick of 925622a (security multi-stage Dockerfile), adapted for OpenAI: no torch, no openai-whisper, no whisper preload

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

### 4c. Image shrink (DONE)

| Win | Status | Saved |
|---|---|---|
| #4 — drop local Whisper, use OpenAI API | ✅ DONE on feat/openai-whisper-swap | ~620 MB compressed (1.7 → 1.08 GB) |
| #2 — multi-stage Dockerfile | ✅ DONE on docs/session-notes ([Dockerfile.multistage](Dockerfile.multistage)) | ~1.76 GB uncompressed (4.82 → 3.08 GB) |
| Drop Playwright + screenshot endpoint | ✅ DONE on docs/session-notes | included in above |

`secured-v4` is the result. Multi-stage discarded ~620 MB of compilers/headers (nasm, ninja-build, cmake, meson, build-essential, all `-dev` libs). Dropping Playwright removed Chromium runtime libs (libnss3, libcups2t64, libgtk-3-0t64, etc.).

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

### Deploy to Cloud Run — current live state

**Live service (as of 2026-05-18):**

| Property | Value |
|---|---|
| Service name | `nca-toolkit` |
| Region | `europe-west1` |
| URL | https://nca-toolkit-108769234292.europe-west1.run.app |
| Image | `us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit:secured-925622a` (local Whisper, multi-stage) |
| Runtime SA | `no-code-toolkit-service-ccount@home-automations-483505.iam.gserviceaccount.com` |
| Memory / CPU | 16Gi / 4 vCPU |
| Timeout | 300s |
| Concurrency | 80 |
| Current revision | `nca-toolkit-00006-xh6` |
| Env vars | `API_KEY` (plain), `GCP_BUCKET_NAME=olah-tv-test-bucket` (plain), `GCP_SA_CREDENTIALS` → Secret Manager |

**Secret Manager:**
- Secret name: `gcp-sa-credentials` (replication=automatic)
- Source: a service-account JSON downloaded locally (since deleted from disk per rotation hygiene)
- Bound to env var `GCP_SA_CREDENTIALS` at `:latest` — Cloud Run injects the JSON content at boot, which is what [services/gcp_toolkit.py:47](services/gcp_toolkit.py#L47) expects for `json.loads()`.
- IAM: `${runtime-SA}` has `roles/secretmanager.secretAccessor` on this secret only.

**How this was set up** (commands actually run, for replay/audit):
```bash
PROJECT=home-automations-483505
SA="no-code-toolkit-service-ccount@${PROJECT}.iam.gserviceaccount.com"

# 1. Enable Secret Manager API + create the secret from local JSON
gcloud services enable secretmanager.googleapis.com --project="$PROJECT"
gcloud secrets create gcp-sa-credentials \
  --project="$PROJECT" \
  --data-file="$HOME/Downloads/home-automations-483505-f14346514121.json" \
  --replication-policy=automatic

# 2. Grant the runtime SA read access on this secret
gcloud secrets add-iam-policy-binding gcp-sa-credentials \
  --project="$PROJECT" \
  --member="serviceAccount:${SA}" \
  --role=roles/secretmanager.secretAccessor

# 3. Wire the secret in (remove plain env var first — Cloud Run forbids same key with two sources)
gcloud run services update nca-toolkit \
  --project="$PROJECT" --region=europe-west1 \
  --remove-env-vars=GCP_SA_CREDENTIALS \
  --update-secrets=GCP_SA_CREDENTIALS=gcp-sa-credentials:latest

# 4. Strip stale GOOGLE_APPLICATION_CREDENTIALS env var (was holding JSON inline; not used by this app)
gcloud run services update nca-toolkit \
  --project="$PROJECT" --region=europe-west1 \
  --remove-env-vars=GOOGLE_APPLICATION_CREDENTIALS
```

**Key rotation:**
```bash
gcloud secrets versions add gcp-sa-credentials --data-file=./new-sa.json
gcloud run services update nca-toolkit --project=home-automations-483505 --region=europe-west1
# Last command forces re-pull of :latest on next revision.
```

**Smoke probe after deploy:**
```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  https://nca-toolkit-108769234292.europe-west1.run.app/v1/toolkit/test
# Expect 401 (auth required). Hit with `-H "X-API-Key: ..."` to exercise GCS upload path.
```

**Caveats on current sizing:**
- Concurrency=80 on a local-Whisper image is aggressive — Whisper-base saturates 4 vCPU on a single transcription. If you see request pile-up and 5xxs under load, drop to `--concurrency=1` (matches the planning recommendation below) or switch to the OpenAI variant.
- Timeout=300s caps any single request at 5 min. Long transcriptions will 504. Either raise to `--timeout=3600` (60-min Cloud Run Services max) or use Cloud Run Jobs via `GCP_JOB_NAME`.

---

### Deploy plan — alt layout (two services, not yet executed)

Original plan was two services so each variant scales independently. Kept here for reference; only `nca-toolkit` (single service running local-Whisper) is live today.

**Common prep (one-time):**
```bash
# 1. Service account for the apps. Needs storage.objectAdmin on the bucket.
SA=nca-toolkit@home-automations-483505.iam.gserviceaccount.com
gcloud iam service-accounts create nca-toolkit \
  --project=home-automations-483505 \
  --display-name="NCA toolkit Cloud Run runtime"

gsutil iam ch serviceAccount:${SA}:objectAdmin gs://YOUR-BUCKET-NAME

# 2. Service-account key JSON (because config.py reads GCP_SA_CREDENTIALS as a string).
#    For tighter posture later, swap to Workload Identity + remove this env var.
gcloud iam service-accounts keys create ./nca-sa.json --iam-account=${SA}
GCP_SA_JSON=$(cat ./nca-sa.json | tr -d '\n')
```

**Service 1 — `nca-local` (local Whisper, secured-925622a):**
```bash
AR=us-docker.pkg.dev/home-automations-483505/no-code-architect-tool-box/nca-toolkit

gcloud run deploy nca-local \
  --project=home-automations-483505 \
  --image=${AR}:secured-latest \
  --region=us-central1 \
  --platform=managed \
  --service-account=${SA} \
  --memory=8Gi --cpu=4 \
  --timeout=3600 \
  --concurrency=1 \
  --max-instances=3 \
  --allow-unauthenticated \
  --set-env-vars=API_KEY=YOUR-API-KEY,GCP_BUCKET_NAME=YOUR-BUCKET-NAME,GUNICORN_WORKERS=2,GUNICORN_TIMEOUT=3000 \
  --set-env-vars=^@^GCP_SA_CREDENTIALS=${GCP_SA_JSON}
```

Notes:
- `--concurrency=1` because Whisper-base + ffmpeg saturate 4 vCPU on one request. Multiple concurrent requests would thrash.
- `--timeout=3600` is the Cloud Run Services max (60 min). If a job needs longer, switch to Cloud Run Jobs (set `GCP_JOB_NAME`).
- `^@^` is a gcloud env-var-list delimiter override — needed because the SA JSON contains commas.

**Service 2 — `nca-openai` (OpenAI Whisper, secured-openai-3ed5951):**
```bash
gcloud run deploy nca-openai \
  --project=home-automations-483505 \
  --image=${AR}:secured-openai-latest \
  --region=us-central1 \
  --platform=managed \
  --service-account=${SA} \
  --memory=2Gi --cpu=2 \
  --timeout=900 \
  --concurrency=4 \
  --max-instances=10 \
  --allow-unauthenticated \
  --set-env-vars=API_KEY=YOUR-API-KEY,OPENAI_API_KEY=sk-...,GCP_BUCKET_NAME=YOUR-BUCKET-NAME,GUNICORN_WORKERS=2,GUNICORN_TIMEOUT=600 \
  --set-env-vars=^@^GCP_SA_CREDENTIALS=${GCP_SA_JSON}
```

Notes:
- Smaller resources because the heavy work (transcription) is offloaded to OpenAI. ffmpeg burn-in is the bulk of local CPU.
- `--timeout=900` (15 min) is plenty: OpenAI transcription takes ~1 min for 60-min audio. ffmpeg burn-in is the bound.
- `--concurrency=4` because the workers are mostly I/O-waiting on OpenAI, not CPU.

**Smoke test after deploy:**
```bash
URL=$(gcloud run services describe nca-openai --region=us-central1 --format='value(status.url)')
curl -X POST "${URL}/v1/toolkit/test" -H "X-API-Key: YOUR-API-KEY"
```

**Rollback:** redeploy with the previous tag. Both `original-*` and `openai-whisper-*` are still in Artifact Registry.

For jobs that genuinely need >60 min (e.g. multi-hour local-Whisper transcription), use Cloud Run Jobs with `GCP_JOB_NAME` instead of Services.

---

## 7. Security hardening (DONE on docs/session-notes)

Triaged a 21-CVE scan against the original image. End state: `nca-toolkit:secured-v4` has **0 CRITICAL, 6 HIGH** — all 6 are upstream-unfixed and assessed unreachable in this app (mitigation notes below).

### 7a. What changed

Single artifact: [Dockerfile.multistage](Dockerfile.multistage). Plus deletions: [routes/v1/image/screenshot_webpage.py](routes/v1/image/screenshot_webpage.py), [services/v1/image/screenshot_webpage.py](services/v1/image/screenshot_webpage.py).

Five remediation moves:

1. **Multi-stage split.** Builder stage compiles ffmpeg/libass stack and is discarded. Runtime stage gets only shared libraries — no compilers, no `ninja-build`. Closes CVE-2026-7210 (python3.13 was pulled in by `ninja-build`→`python3`), CVE-2026-23949 (`python3-jaraco.context` was pulled in by python3), CVE-2026-6069 (nasm builder-only).
2. **Playwright dropped.** Removed Chromium runtime libs (libnss3, libcups2t64, libatk1.0-0t64, libgtk-3-0t64 etc.) plus pip + `playwright install` + the two screenshot files. Closes CVE-2026-6766/6772 (nss), CVE-2026-34980 (cups), CVE-2026-40393 (mesa was a transitive of the Chromium stack).
3. **Base packages upgraded.** Added `apt-get -y upgrade` to the runtime stage. Pulled trixie 13.4 → 13.5 point release. Closes CVE-2026-4878 (libcap2), CVE-2026-29111 (systemd), CVE-2026-4046 / CVE-2026-4437 (glibc), CVE-2026-6732 (libxml2), CVE-2026-5121 / CVE-2026-4111 / CVE-2026-4424 (libarchive).
4. **Python pip pins**: `wheel>=0.46.2`, `setuptools>=80.0`, `jaraco.context>=6.1.0`. Latest setuptools also vendors the patched jaraco.context internally. Closes CVE-2026-24049 (wheel) and seals the jaraco.context CVE both at top-level and vendored-inside-setuptools.
5. **Gnutls removed.** ffmpeg rebuilt without `--enable-gnutls`. SRT switched to its openssl backend (`-DUSE_ENCLIB=openssl`). `wget` removed (its libgnutls30t64 dep). Closes all 5 gnutls28 CVEs: CVE-2026-42010, -33845, -42011, -33846, -3833.

Also: dropped `--enable-libtheora` from the ffmpeg configure line and `libtheora-dev` from apt. Closes CVE-2026-5673.

### 7b. Why gnutls removal was safe (hypothesis test)

Static analysis: every `ffmpeg.input(...)` callsite in the app receives a local filesystem path. URL fetches go through Python `requests` (OpenSSL via stdlib `ssl`), not ffmpeg. Zero RTMP / SRT-URL / DTLS / SRTP references in the codebase.

Runtime test (on `secured-v2`, before gnutls removal): traced ffmpeg processing a local file with strace + ltrace. **0 TCP connections, 0 `AF_INET` sockets**. The only gnutls calls were `gnutls_global_init()` / `gnutls_global_deinit()` — lifecycle hooks that run unconditionally because ffmpeg's protocol registry initializes the TLS handler at startup. **No `gnutls_handshake()`, no `gnutls_dtls_*()`, no `gnutls_x509_*()` validation** — and those are precisely where the 5 CVEs live.

Removing `--enable-gnutls` removed the library linkage entirely.

### 7c. Remaining 6 HIGH findings (accepted)

All upstream-unfixed at scan time. Mitigation rationale:

| Package | CVE | Why unreachable here |
|---|---|---|
| libexpat1 ×2 | CVE-2026-25210, CVE-2026-45186 | Pulled in by `libfontconfig1`. Expat only parses static `/etc/fonts/*.conf` system files. Zero `import xml` / XML parsing in app code — JSON throughout. No attacker-controlled XML reaches expat. |
| ncurses ×4 (libncursesw6, libtinfo6, ncurses-base, ncurses-bin) | CVE-2025-69720 | Buffer overflow exploitable only via crafted terminal escape sequences to a curses process. App doesn't use ncurses; Cloud Run containers have no TTY; nothing in the request path reaches a curses consumer. |

Re-check on next scheduled rebuild (Debian usually ships fixes within weeks).

### 7d. Build + scan reference

```bash
# Build
docker buildx build --platform linux/amd64 -f Dockerfile.multistage -t nca-toolkit:secured-v4 .

# Verify gnutls + python3.13 gone
docker run --rm nca-toolkit:secured-v4 bash -c "dpkg -l | grep -E 'python3|gnutls' || echo CLEAN"

# Scan
trivy image --severity CRITICAL,HIGH --scanners vuln nca-toolkit:secured-v4
```

---

## 8. Open items / next steps

Resolved this session:
- ✅ Whisper migration scope (all three files swapped)
- ✅ Apply changes (committed to `feat/openai-whisper-swap`)
- ✅ Build images and push to Artifact Registry
- ✅ Smoke tests pass for both
- ✅ Fail-fast validation for `OPENAI_API_KEY`
- ✅ Multi-stage Dockerfile rewrite ([Dockerfile.multistage](Dockerfile.multistage))
- ✅ Playwright dropped
- ✅ CVE remediation (20/21 closed, 6 remaining all unreachable)
- ✅ Multi-stage applied to both variants. `secured-925622a` (local) + `secured-openai-3ed5951` (OpenAI) pushed to Artifact Registry as `secured-latest` and `secured-openai-latest`.
- ✅ Cloud Run deploy — `nca-toolkit` live in `europe-west1` on `secured-925622a`. GCS credentials via Secret Manager (`gcp-sa-credentials`). HTTP 401 verified.

Still open:
1. **Cloud Run deploy — OpenAI variant** — not yet deployed. Plan for `nca-openai` in §6 alt layout. Currently only the local-Whisper variant (`nca-toolkit`) is live.
2. **End-to-end GCS upload test** — health probe returns 401 (auth works), but no authenticated call has been made yet to exercise the secret + bucket path. Run with `-H "X-API-Key: ..."` against `/v1/toolkit/test` (or a real endpoint that uploads to GCS) to confirm the SA secret actually loads + uploads succeed.
3. **Tighten sizing on `nca-toolkit`** — current concurrency=80 with local Whisper will pile up under load. Drop to `--concurrency=1` or switch to OpenAI variant.
4. **Cloud Run timeout** — currently 300s. Long transcriptions will 504. Raise to 3600 if running local Whisper end-to-end.
5. **Job-file cleanup snippet** in [app.py](app.py) — §4a above. RAM leak in production otherwise. (Deferred.)
6. **Audio chunking** for >52-min files on the OpenAI variant (25 MB upload cap at 64 kbps).
7. **Merge `feat/openai-whisper-swap` → `docs/session-notes` (or main)** if ready to make OpenAI the default.
8. **Refactor GCP auth to support ADC** — currently `GCP_SA_CREDENTIALS` is required everywhere ([config.py:38](config.py#L38), [services/gcp_toolkit.py:37](services/gcp_toolkit.py#L37), [services/gcp_toolkit.py:78](services/gcp_toolkit.py#L78), [services/v1/gcp/upload.py:30](services/v1/gcp/upload.py#L30), [routes/gdrive_upload.py:42](routes/gdrive_upload.py#L42)) and must hold the **JSON content as a string**, not a file path. Cloud Run already injects an identity via the attached service account — fall back to `google.auth.default()` when `GCP_SA_CREDENTIALS` is unset and `validate_env_vars('GCP')` only requires `GCP_BUCKET_NAME`. Removes a long-lived secret from runtime config.
9. **Surface silent GCS init failure** — [services/gcp_toolkit.py:53-54](services/gcp_toolkit.py#L53-L54) swallows the real error and returns `None`, so any misconfig (path-instead-of-JSON, malformed JSON, wrong SA) surfaces only as "GCS client is not initialized" later. Add `exc_info=True` and re-raise on `ValueError`/`json.JSONDecodeError` at minimum.

---

## 9. Key file paths for VS Code

- [app.py](app.py) — Flask app, queue, decorators
- [app_utils.py](app_utils.py) — `validate_payload`, `log_job_status`, `discover_and_register_blueprints`
- [config.py](config.py) — env var validation (now includes `OPENAI_API_KEY` fail-fast on feat branch)
- [Dockerfile](Dockerfile) — original single-stage build (CPU-torch + pip retries applied)
- [Dockerfile.multistage](Dockerfile.multistage) — secured multi-stage build (no Playwright, no gnutls, no python3.13)
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
