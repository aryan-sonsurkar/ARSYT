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

Tests: `tests/test_clipfree_ingestion.py`, `test_clipfree_transcription.py`, `test_clipfree_studio.py` (**28 passed**), and `tests/e2e_workflow.py` (real media).

## Validation evidence

- Phase 3 quality: `test.wav` English 99.4%; `voices/19_narration.wav` English 99.9%, 5 segments/68 words, RTF 0.159 with VAD on.
- Full E2E project `5f93f9b2230345678f80e974f59d0332`:
  - 6 m 16 s English podcast, 1280×720, 124 MB.
  - Transcript 986 words/79 segments, RTF 0.1706 on `small`/CPU/int8.
  - 4 candidates from `gemma3:4b` in 53.9 s.
  - 3 clips created, 2 rendered: 22.97 s/9.04 MB and 36.40 s/14.43 MB (1080×1920 H.264 + AAC 48 kHz stereo).
  - Metadata generated; single export and project ZIP (`E2E_Local_Podcast-6eaade.zip`, 23.4 MB) succeeded.
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
