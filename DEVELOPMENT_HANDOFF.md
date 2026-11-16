# ClipFree Development Handoff

Updated: 2026-11-13

## Current state

ClipFree is feature-complete for the agreed local-only scope: ingestion → transcription → highlight review → vertical render (burned-in captions, optional music) → metadata/thumbnail → export. All seven phases are recorded as implemented in `IMPLEMENTATION_STATUS.md`, with a real end-to-end run and focused tests.

- Repository: `codeshortsbot/` (now a Git repo). Remote: `https://github.com/aryan-sonsurkar/ARSYT.git`.
- Frontend: Next.js 14 at `http://127.0.0.1:3000`.
- Backend: FastAPI at `http://127.0.0.1:8026`.
- Ollama: `http://127.0.0.1:11434`, default model `gemma3:4b`.
- Data: `clipfree_data/` (Git-ignored), separate from legacy `database/codeshorts.db`.
- Python: project `.venv`, Python 3.11.9.

## What was built

Backend modules: `ingestion.py`, `transcription.py`, `ollama.py`, `highlights.py`, `captions.py`, `reframe.py`, `presets.py`, `media.py`, `rendering.py`, `metadata.py`, `thumbnails.py`, `exporting.py`, plus extended `main.py` and `database.py`.

Frontend: new studio shell in `frontend/src/app/page.tsx`, components under `frontend/src/components/studio/`, typed client in `frontend/src/lib/api.ts`, styles in `frontend/src/app/studio.css`.

Tests: `tests/test_clipfree_ingestion.py`, `test_clipfree_transcription.py`, `test_clipfree_studio.py` (**42 passed**), and `tests/e2e_workflow.py` (real media).

## YuNet face detection

`backend/reframe.py` now detects faces with OpenCV's `FaceDetectorYN` (YuNet) before falling back to the classic Haar cascade and finally to a deterministic centre crop. The 232 KB ONNX model is fetched once into `clipfree_data/models/yunet/` and reused for every clip; detection runs at ~1 sample/second (capped at 90 samples per clip), picks the largest face per frame and the track is median-smoothed before the piecewise pan/zoom FFmpeg expressions are built. YuNet also works on OpenCV 5 builds where the Haar cascade API no longer exists, so smart reframe is functional again on modern installs.

## Metadata-first thumbnails

`ClipService.run` generates the clip metadata before the thumbnail so the AI-chosen `thumbnail_text` (a 3–6 word hook) is burned onto the thumbnail JPG instead of the plain clip title. Regenerating metadata from the studio (`POST /api/clips/{id}/metadata`) also refreshes the burned-in thumbnail text on the existing render.

## Language picker and Hindi captions

The transcription panel's free-text language field is now a dropdown of common podcast languages (auto-detect, English, Hindi/Hinglish, Marathi, Bengali, Tamil, Telugu, Urdu, Gujarati, Punjabi plus major world languages); the chosen code flows through the existing `language` field. Caption rendering auto-detects Devanagari characters in the clip's transcript and switches the ASS style font to **Nirmala UI** (ships with Windows) while disabling uppercasing for those cues, so Hindi/Hinglish captions render correctly instead of tofu boxes; English captions keep the style's own font.

## Platform caption presets

`CaptionOptions.platform` (`youtube` / `reels`) shifts captions above the platform's UI overlays: `_position_y` adds a 150 px bottom margin for Reels because Instagram's username/caption/music strip covers more of the lower frame than the YouTube Shorts interface. The render dialog exposes it as a "Platform safe area" dropdown and stores it in the clip's caption options, so re-renders keep the choice.

## Silence trimming (opt-in)

`backend/silence.py` (`plan_trim`) finds gaps longer than 1.2 s between transcript words inside a clip and builds kept pieces that retain a natural ~0.35 s pause (a 0.05 s guard stops the first phoneme from being clipped). `ClipService._render_video` renders those pieces with FFmpeg `trim` (video) / `atrim` (audio) + `concat` chains and re-times the transcript onto the trimmed timeline (`_remap_content`), so burned-in captions and SRTs stay word-synchronised on the shorter video. The whole feature is behind the "Trim dead air" toggle in the render dialog (`RenderRequest.trim_silence`); clip source timestamps in the database stay untouched, and the plan is skipped entirely when no gap is long enough to matter. A real-FFmpeg smoke test (30 s source → 25.4 s output) validated the generated chains.

## A/B title variants

`backend/metadata.py` asks the local model for three alternative titles in different angles and stores them as `title_variants` (main title first, duplicates/blank entries dropped, each capped at 80 characters). The local fallback also produces variants from the excerpt so the studio always has choices offline. The clip metadata dialog renders every variant with a "Use" button that loads it into the editable title field — applying a variant is explicit and saved with "Save title".

## YouTube publish queue (manual confirm only)

`backend/publishing.py` uploads rendered clips through the official YouTube Data API v3 with the user's own Google Cloud OAuth client only — no credentials ship with ClipFree. Setup: save `client_secret.json` (an installed-app OAuth client with the `youtube.upload` scope) to `clipfree_data/publishing/client_secret.json`, press "Connect YouTube account", and finish Google's consent in the browser (loopback redirect `http://127.0.0.1:8026/api/publishing/auth/callback`). The token is stored locally at `clipfree_data/publishing/token.json` and refreshed transparently.

Publishing is a durable job (kind `publish`) with resumable chunked upload and live progress. The dialog never uploads until the user types `PUBLISH` into the confirm field; title/description/tags/visibility/category are editable and `#Shorts` is appended automatically. The clip stores the resulting YouTube URL in its metadata. Interrupted uploads are failed on backend restart and retryable from the queue.

## Natural cut boundaries

`backend/boundaries.py` keeps clips from cutting mid-word or mid-thought. It collects every pause in the transcript (silence between words ≥ 0.35 s) and every sentence end, then moves a requested cut to the nearest one within a small window (ends ±2.0/2.5 s, starts ±1.5/2.0 s; sentence boundaries preferred slightly over pauses; the original timing wins when nothing meaningful is nearby and clips never shrink below their minimum length).

Snapping is applied in three places: `highlights._snap_to_transcript` (AI candidates, candidate edits, manual clips), `ClipService.update_clip` (studio time edits) and `ClipService.run` (a final safety at render time that also persists the aligned times back to the clip). Re-rendering an older clip therefore cleans up its cuts automatically.

## One-click full pipeline

`backend/pipeline.py` (`PipelineService`) runs a whole project in one `pipeline` job: transcription (Whisper `small` by default) → highlight detection with the configured Ollama model → approval of the top-ranked candidates (only when the project has no clips yet) → serialized rendering of every draft clip with the standard render settings → local export of each rendered clip plus a project ZIP written to `clipfree_data/projects/<id>/exports/`.

Every stage is a real job (`transcription`, `highlights`, `render`) that appears in the job list with its own cancel and retry; the parent job mirrors each stage's progress. Stages are idempotent — an existing transcript, existing candidates and already rendered clips are never redone — so starting the pipeline again after a failure resumes where it stopped. Cancelling the pipeline job cancels the active stage, and `recover_interrupted_pipelines()` marks orphaned runs failed on server restart. The studio exposes it via `POST /api/projects/{id}/pipeline` and a "Run full pipeline" button in the project's Overview Pipeline panel; Whisper/Ollama model availability is validated up front so the run fails fast with a clear message.

## Viral caption writer

`backend/social.py` writes five trending caption variants per rendered clip (curiosity gap, contrarian take, number shock, story opener, save & share) with the local Ollama model, grounded in that clip's own transcript excerpt so it never invents facts. A deterministic writer keeps the feature usable when Ollama is offline.

- `GET /api/clips/{id}/social` - saved variants
- `POST /api/clips/{id}/social` - write new variants (`{"model": "gemma3:4b", "count": 5}`)
- `POST /api/clips/{id}/social/save` - persist edited variants

In the studio every rendered clip gets a flame action that opens the panel: each variant is editable and has a Copy button that puts the hook, caption, hashtags and CTA on the clipboard. Captions are text only - ClipFree never publishes anything.

## Validation evidence

- Phase 3 quality: `test.wav` English 99.4%; `voices/19_narration.wav` English 99.9%, 5 segments/68 words, RTF 0.159 with VAD on.
- Full E2E project `e259be863a784c1f830f3101db4eea63` (run against the shipped commit):
  - 6 m 16 s English podcast, 1280×720, 124 MB.
  - Transcript 986 words/79 segments, RTF 0.1702 on `small`/CPU/int8.
  - 4 candidates from `gemma3:4b` in 52.8 s.
  - 3 clips created, 2 rendered: 13.10 s/5.19 MB and 24.27 s/9.62 MB (1080×1920 H.264 + AAC 48 kHz stereo).
  - Metadata generated; single export and project ZIP (`E2E_Local_Podcast-3f2436.zip`) succeeded.
  - Persistence re-verified after a backend restart: project, transcript, candidates, clips and rendered metadata all survived.
- Captions visually confirmed burned into extracted frames: opaque white fill, yellow word highlight, no edge clipping, no overlapping cues.
- Frontend production build (`npm run build`) succeeds; browser smoke test of Overview/Highlights/Clips reports no console errors.

## Key decisions and gotchas

- Default Whisper model is `small` because it is the only installed/validated model; do not silently download others.
- Captions are burned with FFmpeg `ass=filename=...` and `cwd=clip_dir` to avoid Windows path-escaping problems.
- ASS text colours must stay opaque (`\c&H00…`): an `&HFF` alpha renders an invisible fill.
- Long caption lines are auto-shrunk per cue (`\fs`) and wrapped inside the 1080 px frame; keep the width estimate in `backend/captions.py` in sync with the font.
- FFmpeg reports `out_time_ms=N/A` before the first frame; progress parsing must skip non-numeric values instead of failing the job.
- Batch routes must be declared before parameterized routes such as `/api/candidates/{id}/{state}` and `/api/clips/{id}/render`.
- OpenCV 5 has no Haar cascades; reframing falls back to centre crop. Do not downgrade OpenCV.
- Renders and heavy transcription are serialized; keep `CLIPFREE_RENDER_CONCURRENCY=1` on this laptop.
- `CLIPFREE_DATA_DIR`, `CLIPFREE_OLLAMA_URL`, `CLIPFREE_WHISPER_MODEL`, `CLIPFREE_RENDER_CONCURRENCY` are the main operational overrides.
- Git ignores `.env`, `clipfree_data/`, `temp/`, `node_modules/`, `.next/`, logs, databases and generated media. `.env.example` is committed.

## Restart commands

Backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8026
```

Frontend:

```powershell
cd frontend
npm run dev
```

Production frontend: `npm run build` then `npm run start`.

## Verification commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_clipfree_ingestion.py tests/test_clipfree_transcription.py tests/test_clipfree_studio.py
.\.venv\Scripts\python.exe tests/e2e_workflow.py
cd frontend; npm run build
```

The E2E script expects `temp/e2e_podcast.mp4` (regenerate with `scripts/generate_test_podcast.py`) and a running backend + Ollama.

## Remaining/optional work

1. Add a modern face detector (YuNet/MediaPipe) to restore smart reframe on OpenCV 5.
2. Add resumable uploads and SSE-driven UI updates.
3. Improve metadata/hashtag tuning with a larger local model if hardware allows (benchmark first).
4. Optional: add a Windows task/launcher for one-command startup.
5. Keep the legacy CodeShortsBot pipeline intact; do not migrate its data into ClipFree.
