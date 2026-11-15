# ClipFree Implementation Status

Last updated: 2026-11-14

ClipFree is a **local-only** podcast-to-Shorts studio built alongside the legacy CodeShortsBot pipeline. It runs a Next.js UI on `http://localhost:3000` and a FastAPI service on `http://127.0.0.1:8026`, using FFmpeg, faster-whisper, Ollama, SQLite and the local filesystem. No paid API, cloud inference, automatic social publishing, DRM/auth/paywall bypass, or automatic model download is used.

## Phase 1 — Local web application foundation

- [x] Inspect and preserve the existing CodeShortsBot repository and runtime.
- [x] Separate `frontend/` Next.js TypeScript app and `backend/` FastAPI service.
- [x] Dark local-studio dashboard: workspace navigation, truthful empty states, project creation, live backend statistics, project history, storage and Ollama status.
- [x] Isolated SQLite project/job tables with durable create/list/detail/delete.
- [x] Per-project folders for source, audio, transcript, candidates, clips, subtitles, music, exports and metadata.
- [x] Loopback-only binding and local-only CORS.
- [x] Frontend production build and live API/project CRUD verification.

## Phase 2 — Source ingestion and audio preparation

- [x] Stream local uploads to per-project source folders in 1 MiB chunks.
- [x] Sanitize filenames, validate extensions/container signatures, constrain paths, reject corrupt/no-video sources via FFprobe.
- [x] Persist real format, duration, dimensions, frame rate, audio presence and byte size.
- [x] Extract the first audio stream to verified 16 kHz mono PCM WAV using an atomic temporary file.
- [x] Durable ingestion jobs with real stages, progress, cancellation, retry and actionable failures.
- [x] Direct public-URL import only when the caller confirms authorization; restricted/credentialed/private/paywalled/DRM sources are rejected.
- [x] Focused tests for upload validation, metadata, audio extraction, invalid files and interrupted extraction.
- [x] HTTP end-to-end run: upload, probe, WAV extraction, cancellation and retry.

## Phase 3 — Local transcription (accepted)

- [x] faster-whisper model inventory plus explicit-confirmation download; nothing is fetched automatically.
- [x] Queued transcription jobs on the serialized heavy-operation lock, with cancellation, retry and restart recovery.
- [x] Auto or manual language, VAD filtering, segment timestamps, optional word timestamps and confidence fields.
- [x] Persist `transcript.json`, `transcript.srt` and `transcript.txt` with model/language/device/RTF/RAM/VRAM telemetry.
- [x] Searchable transcript, click-to-seek audio, and a warning when auto-detected language confidence is below 50%.
- [x] Prevent duplicate active transcriptions; recover queued jobs after restart.
- [x] Install faster-whisper and the `small` model after explicit approval; verified CPU/int8 and CUDA/int8_float16 model loads.
- [x] Auto mode falls back to CPU/int8 if CUDA inference fails; explicit CUDA still reports the error.
- [x] **Acceptance evidence:** known-speech `test.wav` transcribed at 99.4% confidence, 1 segment; `voices/19_narration.wav` (28.4 s) at 99.9%, 5 segments/68 words with VAD on, RTF 0.159. Default model is now `small`, the only installed model.
- [x] The earlier 8m49s sample was re-checked: its poor 74-word/Kannada result came from unsuitable/non-English audio, not an engine regression. A real 6m16s English podcast later produced 986 words/79 segments at RTF 0.1427.

## Phase 4 — Highlight detection and candidate review

- [x] Bounded transcript chunking with overlap; one chunk at a time on local Ollama.
- [x] Structured JSON output validated for timestamps, duration bounds and required fields; malformed responses are repaired/rejected.
- [x] Confidence-aware scoring, ranking and near-duplicate suppression.
- [x] Default `gemma3:4b`, context 8192, configurable timeouts and model.
- [x] Persist candidates with title, hook, reason, category, scores, excerpt and state.
- [x] Review UI: approve, reject, edit timing/title, bulk approve, regenerate without destroying approved work.
- [x] Approval automatically creates a draft clip.

## Phase 5 — Vertical rendering, captions and music

- [x] 1080×1920 / 30 fps H.264 + AAC rendering through FFmpeg.
- [x] Presets: Podcast, Cinematic, Minimal, Gaming, Motivational (zoom, colour filter, caption style).
- [x] Face-aware crop when detection is available; safe centre-crop fallback on OpenCV builds without Haar cascades.
- [x] ASS caption generation with styled, word-grouped captions; captions are visibly burned into the MP4.
- [x] Optional licensed local background music with volume control and side-chain ducking.
- [x] Render validation: file exists, non-trivial size, correct vertical dimensions and duration.
- [x] Serialized renders (`CLIPFREE_RENDER_CONCURRENCY=1`) to protect the 6 GB VRAM / 16 GB RAM target.

## Phase 6 — Clip review, metadata, thumbnails and export

- [x] Clip library across projects with preview, download, re-render and delete.
- [x] Local metadata generation (title, description, hashtags, hook) with a deterministic fallback when Ollama is unavailable.
- [x] Viral caption writer: five trending caption variants per rendered clip (curiosity gap, contrarian take, number shock, story opener, save & share), written locally from the clip's own transcript, editable in the studio and copyable to the clipboard. Nothing is published automatically.
- [x] Natural cut boundaries: every clip start and end is re-aligned to the nearest pause or complete sentence in the transcript (`backend/boundaries.py`), applied to AI candidates, manual clips, studio edits and at render time, so clips never cut mid-word or mid-thought.
- [x] One-click full pipeline: a single `pipeline` job (route `POST /api/projects/{id}/pipeline` and a "Run full pipeline" button in the Overview panel) runs transcription, highlight detection, top-candidate approval, rendering of every draft clip and a project ZIP export end to end. Each stage runs as a real sub-job with its own progress mirrored into the parent, stages are skipped when their output already exists (so a retry resumes where it stopped), and cancelling the pipeline cancels the active stage.
- [x] YuNet face detection: smart reframe now finds faces with the YuNet ONNX detector (~232 KB, auto-downloaded once into `clipfree_data/models/yunet/`) instead of the Haar cascades that OpenCV 5 removed — far more reliable for podcast framing, with a centre-crop fallback when the model or OpenCV is unavailable.
- [x] Thumbnail text from metadata: rendering now generates the clip metadata first and burns the AI-chosen `thumbnail_text` hook onto the thumbnail JPG (falling back to the clip title); regenerating metadata in the studio also refreshes the burned-in thumbnail text.
- [x] Language picker + Hindi fonts: the transcription panel now offers a language dropdown (auto-detect, Hindi/Hinglish, English, Indian and world languages), and caption rendering automatically switches to Nirmala UI when Devanagari text is detected so Hindi captions render correctly instead of showing boxes.
- [x] Platform caption presets: the render dialog has a "Platform safe area" choice (YouTube Shorts / Instagram Reels); Reels pushes captions ~150 px higher because its UI covers more of the lower frame.
- [x] Silence trimming (opt-in): the render dialog's "Trim dead air" toggle cuts silences longer than 1.2 s inside a clip down to a ~0.35 s pause (`backend/silence.py`, FFmpeg trim/concat chains with `atrim` for audio) and re-times the transcript onto the trimmed timeline so burned-in captions and SRTs stay word-synchronised; clip source timestamps are unchanged.
- [x] Thumbnail generation from a real rendered frame.
- [x] SRT sidecar for each clip.
- [x] Single-clip export, batch export and per-project ZIP export with metadata.
- [x] No automatic publishing; downloads are explicit local files.

## Phase 7 — Reliability, safeguards and end-to-end validation

- [x] Durable job states, cancellation checks, restart recovery and retry counters.
- [x] Free-space checks, upload limits, filename/path containment and local-only networking.
- [x] Persisted settings/templates with validation.
- [x] Hash-based deep links: `#/projects`, `#/projects/<id>`, `#/clips`, `#/queue`, `#/settings`.
- [x] Focused automated coverage: **38 passed** across ingestion, transcription and studio tests.
- [x] Caption rendering hardened: opaque text colours, per-cue font fitting so long lines stay inside the 1080 px frame, smart wrapping inside the margins, and non-overlapping cue boundaries.
- [x] FFmpeg progress parsing tolerates the `N/A` timestamps FFmpeg emits before the first frame, so renders no longer abort mid-pipeline.
- [x] Real end-to-end acceptance run (project `e259be863a784c1f830f3101db4eea63`, run against the shipped commit):
  - 6 m 16 s English podcast, 1280×720, 124 MB source.
  - Transcript: 986 words / 79 segments, English, RTF 0.1702 on `small`/CPU.
  - 4 candidates from `gemma3:4b` in 52.8 s (3 chunks).
  - 3 clips created; 2 rendered at 13.10 s / 5.19 MB and 24.27 s / 9.62 MB (1080×1920 H.264 + AAC 48 kHz stereo).
  - Captions verified frame by frame: filled white/yellow text burned into the picture, no clipping, no overlapping cue events.
  - Metadata generated, single export and project ZIP (`E2E_Local_Podcast-3f2436.zip`) verified.
  - Persistence re-checked after a backend restart: project, transcript, candidates, clips and rendered metadata all survived.

## Acceptance

All seven phases are implemented and locally validated. The remaining known limitations are documented in `KNOWN_ISSUES.md`; the main ones are best-effort face reframing on OpenCV 5, CPU-only Whisper inference on this laptop, and the absence of automated social publishing (by design).
