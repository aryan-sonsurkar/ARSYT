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
- [x] Thumbnail generation from a real rendered frame.
- [x] SRT sidecar for each clip.
- [x] Single-clip export, batch export and per-project ZIP export with metadata.
- [x] No automatic publishing; downloads are explicit local files.

## Phase 7 — Reliability, safeguards and end-to-end validation

- [x] Durable job states, cancellation checks, restart recovery and retry counters.
- [x] Free-space checks, upload limits, filename/path containment and local-only networking.
- [x] Persisted settings/templates with validation.
- [x] Hash-based deep links: `#/projects`, `#/projects/<id>`, `#/clips`, `#/queue`, `#/settings`.
- [x] Focused automated coverage: **27 passed** across ingestion, transcription and studio tests.
- [x] Caption rendering hardened: opaque text colours, per-cue font fitting so long lines stay inside the 1080 px frame, and smart wrapping inside the margins.
- [x] FFmpeg progress parsing tolerates the `N/A` timestamps FFmpeg emits before the first frame, so renders no longer abort mid-pipeline.
- [x] Real end-to-end acceptance run (project `5f93f9b2230345678f80e974f59d0332`):
  - 6 m 16 s English podcast, 1280×720, 124 MB source.
  - Transcript: 986 words / 79 segments, English, RTF 0.1706 on `small`/CPU.
  - 4 candidates from `gemma3:4b` in 53.9 s.
  - 3 clips created; 2 rendered at 22.97 s / 9.07 MB and 36.40 s / 14.53 MB (1080×1920 H.264 + AAC 48 kHz stereo).
  - Captions verified frame by frame: filled white/yellow text burned into the picture, no clipping at the frame edges.
  - Metadata generated, single export and project ZIP (`E2E_Local_Podcast-535ba2.zip`, 23.6 MB) verified.
  - Persistence re-checked after a backend restart: project, transcript, 4 candidates, 3 clips and 2 rendered clips with metadata all survived.

## Acceptance

All seven phases are implemented and locally validated. The remaining known limitations are documented in `KNOWN_ISSUES.md`; the main ones are best-effort face reframing on OpenCV 5, CPU-only Whisper inference on this laptop, and the absence of automated social publishing (by design).
