# ClipFree Known Issues and Limits

Last updated: 2026-11-14

## Rendering and reframing

- **Smart reframe is best-effort.** OpenCV 5.0 removed `CascadeClassifier` and the Haar XML data. `backend/reframe.py` detects this and falls back to a safe centre crop instead of crashing. Face-aware tracking is therefore inactive on the current OpenCV build. Do not downgrade OpenCV just for this; a modern detector (e.g. YuNet) can be added later.
- Presets change zoom, colour and caption styling, but do not perform scene detection or multi-subject tracking.
- Captions are generated and burned during render. Editing a clip’s captions or timings requires a re-render.
- Caption line widths are estimated from an average glyph ratio rather than measured font metrics, so an unusually wide cue can still sit close to the frame edge. Captions auto-shrink and wrap inside the margins, and the estimate can be tuned in `backend/captions.py` if a style needs it.
- Rendering is serialized (`CLIPFREE_RENDER_CONCURRENCY=1`). Batch renders queue rather than run in parallel, which is intentional on 6 GB VRAM / 16 GB RAM hardware.
- Render duration depends on source resolution, preset colour filters and CPU. The validated 25 s clip rendered comfortably, but older CPUs will be slower.

## Transcription

- CUDA inference is **unverified** on this laptop: CTranslate2 detects a CUDA device, but the CUDA runtime lacks compatible libraries (e.g. `cublas64_12.dll`). Auto mode falls back to CPU/int8; explicit CUDA reports the error. No large CUDA runtime was downloaded.
- Only the `small` Whisper model is installed. `tiny`, `base` and `medium` are not present; ClipFree will not download them without explicit confirmation.
- The earlier 8m49s “failed” sample was unsuitable/non-English audio. Known English samples validate correctly (99.4% and 99.9% confidence). Keep VAD enabled; disabling it can append a hallucinated trailing sentence after silence.
- Transcription is CPU-only on this machine, so long podcasts take time (validated RTF ≈ 0.14 for a clean 6-minute podcast, but slower hardware/media will vary).

## Highlights and Ollama

- Highlight quality depends on the local model. `gemma3:4b` is the validated default and fits the target hardware; larger models may improve results but increase memory/time.
- Local models can occasionally return malformed or weak JSON. ClipFree validates, clamps and drops invalid candidates, and the deterministic fallback is intentionally conservative. Always review candidates before rendering.
- Highlight jobs are serialized per chunk. A long transcript takes multiple Ollama calls (validated: 3 chunks / 44.9 s).
- Metadata generation falls back to deterministic text when Ollama is unavailable; output will be less creative.

## Ingestion and storage

- Uploads are streamed but not resumable. A dropped browser connection requires starting the import again.
- The upload request stays open until WAV extraction completes, so large files can appear to “hang” in the browser even while durable job progress advances.
- Direct URL import supports public direct media files only. It refuses platform pages, authenticated URLs, paywalls, private-network destinations and DRM content; it never attempts a bypass.
- Project ZIP exports duplicate rendered media under `clipfree_data/exports` and the project folder, so disk use grows with repeated exports.
- `clipfree_data/` was roughly 775 MB after the validation runs and is Git-ignored.

## UI and operations

- The UI polls the REST API every five seconds. Cancellation or completion can take up to one poll interval to appear. `/api/events` exists but the browser still uses polling as its source of truth.
- Hash routing (`#/projects/<id>`) supports deep links but is client-side only; there is no server-side route for those paths.
- If the backend restarts, open the page again or wait for the next poll. Jobs that were running during a crash are marked failed/retryable rather than resumed mid-inference.
- The Google Fonts import needs network access on first load; system sans-serif fallbacks are configured.
- Empty states are intentional before the first project/model exists.

## By design

- No automatic publishing to YouTube, Instagram/TikTok or any other platform.
- No paid APIs, cloud inference, telemetry or uploading of media to third parties.
- No DRM, authentication, paywall or platform-restriction bypass.
- No automatic downloads of Whisper models, Ollama models or CUDA packages.

## Legacy CodeShortsBot

- The legacy scheduled pipeline, dashboard, `database/codeshorts.db`, outputs and browser profiles remain separate and are not migrated into ClipFree.
- Legacy upload automation still exists for the old pipeline; ClipFree does not call it.
