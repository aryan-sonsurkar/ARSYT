# ClipFree Technical Architecture

## Status

ClipFree is a complete local podcast-to-Shorts studio. It runs beside the legacy CodeShortsBot modules without replacing them: the scheduled generator, upload automation, `database/codeshorts.db`, dashboard, reports, outputs and browser profiles remain untouched. ClipFree owns a separate SQLite database and data root.

```text
Browser: http://localhost:3000
  Next.js 14 / React 18 / TypeScript / Tailwind 3 / Framer Motion
  REST polling (5 s) · hash routes · previews and downloads
        │ loopback only
        ▼
FastAPI: http://127.0.0.1:8026
  Projects · jobs · transcription · highlights · clips · render · metadata · exports
  FFprobe + FFmpeg · faster-whisper/CTranslate2 · Ollama HTTP · OpenCV · Pillow · SQLite
        │
        ├── clipfree_data/database/clipfree.db
        ├── clipfree_data/models/whisper/…
        ├── clipfree_data/exports/…
        └── clipfree_data/projects/{project-id}/
              source/ audio/ transcript/ candidates/ clips/ subtitles/
              music/ exports/ metadata/ thumbnails/
```

## Runtime boundaries

- API binds to `127.0.0.1:8026` (`CLIPFREE_HOST`, `CLIPFREE_PORT`); CORS permits only local frontend origins.
- Frontend binds to `127.0.0.1:3000`.
- Ollama is reached at `http://127.0.0.1:11434` by default (`CLIPFREE_OLLAMA_URL` / `CLIPFREE_OLLAMA_TIMEOUT`).
- Data root defaults to `clipfree_data/` and is configurable with `CLIPFREE_DATA_DIR`.
- Legacy CodeShortsBot behavior, data and configuration are preserved.

## Data model

SQLite in WAL mode with foreign keys:

| Table | Purpose |
|---|---|
| `projects` | Source metadata, media probe JSON, status, timestamps |
| `jobs` | Durable ingestion/transcription/render jobs: state, stage, progress, cancellation, retry count, payload |
| `transcripts` | One per project: model, language/confidence, device, compute type, paths, word/segment counts, RTF, RAM/VRAM telemetry |
| `candidates` | Highlight proposals: time range, title, hook, reason, category, scores JSON, overall score, excerpt, review state, source |
| `clips` | Draft/rendered clips: time range, preset, caption style/options, music/volume, output/thumbnail/SRT paths, duration, size, metadata JSON |
| `settings` | Persisted key/value studio settings |

Project IDs are UUID hex values. Every server-derived path is resolved and constrained under the projects root. Project deletion cascades database rows and removes the project directory.

## Pipeline

### 1. Ingestion (`backend/ingestion.py`)

- Uploads stream in 1 MiB chunks into `source/incoming/`, never buffering the full file.
- Filenames are sanitized; extensions and container signatures are checked; FFprobe confirms a real video stream.
- Free-space and size limits are enforced (`CLIPFREE_MAX_UPLOAD_BYTES`, `CLIPFREE_MIN_FREE_BYTES`).
- FFmpeg extracts the first audio stream to 16 kHz mono PCM WAV via an atomic temporary file.
- Direct HTTP(S) media URLs require an explicit authorization acknowledgement. Local/private destinations, credentials in URLs, platform pages and DRM/paywall sources are rejected.

### 2. Transcription (`backend/transcription.py`)

- faster-whisper/CTranslate2; models are installed locally under `clipfree_data/models/whisper` and are **never downloaded automatically**. The UI requires explicit confirmation.
- Default model: `small` (`CLIPFREE_WHISPER_MODEL`), CPU/int8 on this laptop. Auto device retries CPU/int8 if CUDA inference fails; explicit CUDA reports the real error.
- VAD filtering avoids trailing hallucinated segments when the source has silence.
- Outputs `transcript.json`, `.srt`, `.txt` and telemetry (language confidence, elapsed time, RTF, peak RAM/VRAM delta).
- One active transcription per project; jobs are durable and recoverable.

### 3. Highlight selection (`backend/highlights.py`, `ollama.py`)

- Transcript is chunked with overlap and sent to local Ollama one chunk at a time.
- Default `gemma3:4b` with an 8K context; no paid or cloud provider.
- Responses must be JSON with start/end/title/hook/reason/category. Times are clamped, short/long ranges rejected, malformed items repaired or dropped.
- Candidates are scored, ranked and de-duplicated by time overlap, then persisted for human review.
- Approval creates a draft clip; regeneration preserves approved candidates and existing clips.

### 4. Rendering (`backend/rendering.py`, `captions.py`, `reframe.py`, `presets.py`)

- FFmpeg produces 1080×1920 H.264/AAC MP4 at the preset frame rate.
- Presets control zoom, colour filter and caption style; all renders are vertical.
- Reframing uses a face track when detection is available. On OpenCV 5 (which removed `CascadeClassifier`/Haar XML), the code detects the missing capability and cleanly falls back to a centre crop. This is best-effort, not full subject tracking.
- Captions are generated as ASS with styled word groups and burned into the video using FFmpeg’s `ass=` filter, executed with the clip directory as `cwd` to avoid Windows path escaping.
- Optional local music is mixed with volume control and side-chain ducking.
- Validation checks output existence, size, duration and vertical dimensions.
- `CLIPFREE_RENDER_CONCURRENCY` defaults to `1`, serializing heavy renders for the target hardware.

### 5. Metadata, thumbnails and export (`metadata.py`, `thumbnails.py`, `exporting.py`)

- Metadata (title, description, hashtags, hook) is produced by local Ollama with a deterministic fallback when unavailable.
- Thumbnails are extracted from real rendered frames with Pillow.
- Exports copy rendered MP4/SRT/metadata into `clipfree_data/exports` or a per-project ZIP; downloads are explicit local files.
- No automatic social posting exists anywhere in ClipFree.

## Concurrency and resource strategy

- Ingestion/transcription work is serialized through a process-local async lock.
- Renders are bounded by a semaphore (default 1).
- Whisper runs on CPU/int8 when CUDA libraries are incomplete; auto mode never silently claims GPU success.
- Ollama chunking keeps prompts bounded; the default 4B model and 8K context fit a 6 GB VRAM / 16 GB RAM laptop.
- The browser polls durable job records every five seconds; SSE (`/api/events`) exists for lightweight event streaming but polling remains the UI source of truth.

## API surface

- Health/dashboard/settings: `/api/health`, `/api/dashboard`, `/api/settings`.
- Projects/jobs: `/api/projects`, `/api/projects/{id}`, `/api/jobs`, `/api/jobs/{id}/{cancel,retry}`.
- Ingestion: `/api/projects/{id}/source/upload`, `/source/url`, `/source`, `/media/audio`.
- Transcription: `/api/transcription/models`, `/models/{name}/download`, `/api/projects/{id}/transcription`, `/transcript`.
- Highlights: `/api/projects/{id}/highlights`, `/candidates`, `/api/candidates/{id}`, `/candidates/batch/approve`, `/candidates/{id}/{state}`.
- Clips/render: `/api/clips`, `/api/clips/{id}`, `/api/clips/{id}/render`, `/video`, `/thumbnail`, `/captions`, `/metadata`, `/download`.
- Exports: `/api/clips/{id}/export`, `/api/clips/batch/export`, `/api/clips/batch/download`, `/api/projects/{id}/export`, `/api/exports`.

## Frontend structure

- `frontend/src/app/page.tsx` — shell and hash routing (`#/projects`, `#/projects/<id>`, `#/clips`, `#/queue`, `#/settings`, `#/dashboard`).
- `frontend/src/components/studio/` — overview, projects, project detail, clip card/library, job queue, settings, modals.
- `frontend/src/lib/api.ts` — typed local API client, polling helpers and download URLs.
- `frontend/src/app/studio.css` — studio design system.

## Security and safety

- Loopback-only services; no inbound network exposure by default.
- No credentials are stored by ClipFree; `.env` is ignored by Git.
- No DRM, authentication, paywall or platform-restriction bypass.
- No automatic publishing to social platforms.
- No automatic model or CUDA downloads; heavy dependencies require explicit user approval.
