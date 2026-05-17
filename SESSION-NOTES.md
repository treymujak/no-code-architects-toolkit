# Session notes — no-code-architects-toolkit

Working doc capturing a Claude Code session on this repo. Use this to pick up in VS Code without losing context.

**Status as of this file:** no code in the repo has been changed yet. Everything below is plan + reference material.

---

## 1. What this repo is

Self-hosted Flask API ([app.py](app.py)) that bundles **ffmpeg + Whisper + Playwright** into one Docker container to replace paid SaaS (Cloud Convert, JSON2Video, ChatGPT Whisper, PDF.co, Createomate, etc.).

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
   - Otherwise → `whisper.load_model("base").transcribe(..., word_timestamps=True)`
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

**Known gotchas:**
- Video downloaded twice (service + route) — redundant
- Whisper `base` is hardcoded — no API knob to change it
- SRT input locked to `classic` style
- `/tmp/jobs/` grows forever (see fix in §4)

---

## 3. OpenAI Whisper API alternative

Replacement for local Whisper, same downstream shape. Key call:

```python
from openai import OpenAI
client = OpenAI()
with open(audio_path, "rb") as f:
    result = client.audio.transcriptions.create(
        model="whisper-1",
        file=f,
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
    )
# result.words: [{word, start, end}]
# result.segments: [{start, end, text, ...}]
```

**Caveats:**
- 25 MB upload limit → strip audio first (`ffmpeg -vn -acodec libmp3lame -b:a 64k`)
- `whisper-1` is the only model with word-level timestamps
- `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` are more accurate but **no word timestamps** — unusable for karaoke/highlight/word_by_word styles
- Cost: $0.006/min audio

---

## 4. Cloud Run deployment plan

Confirmed: repo runs as-is on Cloud Run. Three things to fix before/while deploying.

### 4a. Job status file cleanup

Cloud Run `/tmp` is RAM-backed tmpfs, so the leak eats memory. Drop this into [app.py](app.py)'s `create_app()` (APScheduler is already in [requirements.txt](requirements.txt)):

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

Optionally also sweep orphaned `.mp4` / `.ass` debris.

For production-grade: move `log_job_status()` in [app_utils.py:49](app_utils.py:49) off the filesystem entirely (Firestore / Redis). Bigger change, not blocking.

### 4b. Cost estimate for a 2 GB file (~60 min 1080p)

Cloud Run `us-central1`, late-2025 pricing:

| Item | Estimate |
|---|---|
| Whisper `base` transcription, 60min, 4 vCPU | ~30 min wall, ~$0.17 CPU |
| Memory: 8 GiB × 1,800s | ~$0.04 |
| ffmpeg burn-in pass (re-encode 60min 1080p) | ~$0.10 |
| GCS storage of 2 GB result, 1 month | ~$0.04 |
| Egress when user downloads result (2 GB) | ~$0.17 |
| **Per-job total** | **~$0.50** |

Dominant cost is egress, not compute.

**Critical:** Cloud Run **Services** max request timeout = 60 min (default 5). A 2 GB Whisper-base job will hit it. Use **Cloud Run Jobs** (24h max) via `GCP_JOB_NAME`, **or** swap to OpenAI Whisper API to push transcription off-box (1–2 min wall time).

### 4c. Image shrink — quick wins #2 and #4

Current image: ~4–6 GB. Combined target: ~700–900 MB.

#### Quick win #4 — drop local Whisper, swap to OpenAI API

Files to change:

1. **[requirements.txt](requirements.txt)** — remove `openai-whisper`, `torch`. Add `openai>=1.40`.
2. **[Dockerfile](Dockerfile)** — remove:
   - `pip install openai-whisper`
   - `RUN python -c "...whisper.load_model('base')"` preload line
   - `ENV WHISPER_CACHE_DIR`
   - `RUN mkdir -p ${WHISPER_CACHE_DIR}`
3. **[services/ass_toolkit.py:65](services/ass_toolkit.py:65)** — rewrite `generate_transcription()`:
   - Extract audio to 64 kbps mp3 first (25 MB limit)
   - Call `client.audio.transcriptions.create(model="whisper-1", response_format="verbose_json", timestamp_granularities=["word","segment"])`
   - Reshape response → existing `{segments: [{start, end, text, words: [{word, start, end}]}]}` shape. Downstream code unchanged.
4. **[services/v1/media/media_transcribe.py](services/v1/media/media_transcribe.py)** — same swap if it uses local Whisper. **Need to verify scope** (not yet read in this session).
5. **Env var** — `OPENAI_API_KEY` must be set on Cloud Run service.

Savings: ~1 GB (torch alone).

#### Quick win #2 — multi-stage Dockerfile

Skeleton:

```dockerfile
# ===== Builder stage =====
FROM python:3.10-slim AS builder

# [keep current apt build deps + all the source compiles for srt,
#  svt-av1, vmaf, fdk-aac, libunibreak, libass, ffmpeg, lines 5-153]

# ===== Runtime stage =====
FROM python:3.10-slim

# Runtime-only apt packages (no -dev, no build tools):
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-liberation fontconfig \
    libssl3 libvpx7 libx264-164 libx265-199 libnuma1 \
    libmp3lame0 libopus0 libvorbis0a libtheora0 libspeex1 \
    libfreetype6 libgnutls30 libaom3 libdav1d6 libzimg2 libwebp7 \
    libfribidi0 libharfbuzz0b \
    # Chromium runtime libs — drop if removing Playwright:
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxcomposite1 libxrandr2 libxdamage1 libgbm1 libasound2 \
    libpangocairo-1.0-0 libpangoft2-1.0-0 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/ffmpeg  /usr/local/bin/
COPY --from=builder /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=builder /usr/local/lib/        /usr/local/lib/
COPY --from=builder /usr/share/fonts/custom /usr/share/fonts/custom
RUN ldconfig && fc-cache -f -v

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install jsonschema
# Add playwright install only if keeping screenshot endpoint

RUN useradd -m appuser && chown appuser:appuser /app
USER appuser

COPY . .
EXPOSE 8080
ENV PYTHONUNBUFFERED=1

RUN echo '#!/bin/bash\n\
gunicorn --bind 0.0.0.0:8080 \
    --workers ${GUNICORN_WORKERS:-2} \
    --timeout ${GUNICORN_TIMEOUT:-300} \
    --worker-class sync \
    --keep-alive 80 \
    --config gunicorn.conf.py \
    app:app' > /app/run_gunicorn.sh && chmod +x /app/run_gunicorn.sh

CMD ["/app/run_gunicorn.sh"]
```

Savings: ~1.5 GB more (no build-essential, cmake, git, nasm, autoconf, dev headers in final image).

**Order of operations:** do #4 first (Python edits, easy to test), then #2 (Dockerfile rewrite, needs clean build to verify).

---

## 5. FFmpeg version

Repo currently pins `n7.0.2` at [Dockerfile:119](Dockerfile:119) — built from `git.ffmpeg.org/ffmpeg.git` (mirrored at github.com/FFmpeg/FFmpeg).

Latest releases as of this session:
- **n8.1.1** — newest (8.1 "Karpov" line)
- **n8.0.2** — patch on 8.0 line
- **n7.1.4** — latest on the current 7.x line

Safe incremental upgrade: `n7.1.4`. Major-bump option: `n8.1.1` (test caption endpoint after — major version bumps can shift filter behavior).

### ASS_FEATURE_WRAP_UNICODE

This is a **libass** feature, not an ffmpeg flag. The Dockerfile already handles it correctly at [lines 96-114](Dockerfile:96):

1. Compiles `libunibreak` from upstream
2. Compiles `libass` with `./configure --enable-libunibreak`
3. Compiles ffmpeg with `--enable-libass`

So any clean build of this repo gets ASS_FEATURE_WRAP_UNICODE. Changing the ffmpeg version doesn't break this.

### JVS prebuilt verification script

If considering John Van Sickle static binary instead of source compile, this script verifies libunibreak presence on macOS:

```bash
#!/usr/bin/env bash
set -e
# Requires: xz (brew install xz), curl. Step 3 also needs Docker.

WORK=$(mktemp -d)
curl -fsSL -o "$WORK/ff.tar.xz" \
  https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xJf "$WORK/ff.tar.xz" -C "$WORK"
FF=$(ls -d "$WORK"/ffmpeg-*-amd64-static)/ffmpeg

echo "→ libass build flags:"
strings "$FF" | grep -iE "libass|--enable-libass|--enable-libunibreak" | sort -u

echo "→ libunibreak symbol check:"
HITS=$(strings "$FF" | grep -ic unibreak || true)
if [ "$HITS" -gt 0 ]; then
  echo "✅ libunibreak present ($HITS hits)"
else
  echo "❌ libunibreak NOT found — ASS_FEATURE_WRAP_UNICODE unavailable"
fi

echo "→ Functional test (needs Docker, JVS binary is Linux ELF):"
if command -v docker >/dev/null 2>&1; then
  cat > "$WORK/t.ass" <<'EOF'
[Script Info]
ScriptType: v4.00+
PlayResX: 384
PlayResY: 288
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,日本語テスト wrap check
EOF
  docker run --rm --platform linux/amd64 -v "$WORK:/w" debian:slim bash -c \
    "/w/$(basename $(dirname $FF))/ffmpeg -f lavfi -i color=c=black:s=384x288:d=1 -vf subtitles=/w/t.ass -f null - 2>&1 | tail -20"
fi
```

If JVS doesn't ship libunibreak, fall back to compiling just libass + ffmpeg in the builder stage.

---

## 6. Build / run reference

```bash
# Build (15–30 min first time — compiles ffmpeg from source)
docker build -t nca-toolkit .

# Run with S3 storage
docker run -d -p 8080:8080 --name nca \
  -e API_KEY=pick-any-string \
  -e S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com \
  -e S3_ACCESS_KEY=xxx \
  -e S3_SECRET_KEY=xxx \
  -e S3_BUCKET_NAME=your-bucket \
  -e S3_REGION=nyc3 \
  -e GUNICORN_WORKERS=2 \
  -e GUNICORN_TIMEOUT=300 \
  nca-toolkit

# Smoke test
curl -X POST http://localhost:8080/v1/toolkit/test \
  -H "X-API-Key: pick-any-string"
```

Apple Silicon: add `--platform linux/amd64` (Dockerfile pins x86_64 paths).

Local-dev option: `docker compose -f docker-compose.local.minio.n8n.yml up -d` → API on :8080, MinIO console on :9001, n8n on :5678.

---

## 7. Open decisions / next steps

When resuming, the live choices are:

1. **Apply changes where?**
   - A) Edit `/Users/trey/Documents/Code-repo/no-code-architect-toolkit/no-code-architects-toolkit/` directly (outside any worktree — changes won't be branched)
   - B) Get diffs to apply yourself
   - C) Copy repo into a worktree first for a reviewable branch
2. **Keep Playwright?** Drop only if `/v1/image/screenshot/webpage` isn't needed (~500 MB savings).
3. **FFmpeg version bump?** `n7.0.2` → `n7.1.4` (safe) or `n8.1.1` (test caption first).
4. **Whisper migration scope** — confirm whether [services/v1/media/media_transcribe.py](services/v1/media/media_transcribe.py) needs the same OpenAI swap. **Not yet read.**

### Recommended order when resuming

1. Read [services/v1/media/media_transcribe.py](services/v1/media/media_transcribe.py) to confirm Whisper-swap scope.
2. Apply quick win #4 (Whisper → OpenAI API):
   - Edit `requirements.txt`, `Dockerfile`, `services/ass_toolkit.py`, `services/v1/media/media_transcribe.py`
   - Build, test `/v1/video/caption` and `/v1/media/transcribe` end-to-end
3. Apply quick win #4a (cleanup snippet into `app.py`)
4. Apply quick win #2 (multi-stage Dockerfile)
5. Optionally bump ffmpeg to `n7.1.4`
6. Build final image, push to Artifact Registry, deploy to Cloud Run with `GCP_JOB_NAME` set for long jobs

---

## 8. Key file paths for VS Code

- [app.py](app.py) — Flask app, queue, decorators
- [app_utils.py](app_utils.py) — `validate_payload`, `log_job_status`, `discover_and_register_blueprints`
- [config.py](config.py) — env var validation
- [Dockerfile](Dockerfile) — build (target of quick win #2)
- [requirements.txt](requirements.txt) — Python deps (target of quick win #4)
- [services/ass_toolkit.py](services/ass_toolkit.py) — caption pipeline (target of quick win #4)
- [services/cloud_storage.py](services/cloud_storage.py) — S3/GCS abstraction
- [services/authentication.py](services/authentication.py) — `X-API-Key` check
- [services/webhook.py](services/webhook.py) — async result POST
- [routes/v1/video/caption_video.py](routes/v1/video/caption_video.py) — caption route
- [services/v1/media/media_transcribe.py](services/v1/media/media_transcribe.py) — transcribe service (verify Whisper swap scope)
- [CLAUDE.md](CLAUDE.md) — already documents architecture; useful Claude-Code context
