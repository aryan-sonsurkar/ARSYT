# ClipFree Technical Architecture

## Current Phase: 3 — Local transcription (implementation present; real-model validation pending)

ClipFree is being built alongside the original CodeShortsBot modules. The legacy scheduled script generator, upload automation, database, dashboard, reports, outputs, and browser profiles remain available. ClipFree owns a separate SQLite database and project directory so its schema and local media lifecycle do not interfere with those existing features.

```text
Browser: http://localhost:3000
  Next.js 14 / React 18 / TypeScript / Tailwind 3 / Framer Motion
  REST calls, 5 second dashboard refresh · searchable transcript viewer
        │ localhost only
        ▼
FastAPI: http://127.0.0.1:8026
  Project/job API · ingestion/transcription workers · FFprobe · FFmpeg · faster-whisper · SQLite · local filesystem
  Ollama health via local HTTP API
        │
        ├── clipfree_data/database/clipfree.db
        └── clipfree_data/projects/{project-id}/{source,audio,transcript,candidates,clips,subtitles,music,exports,metadata}
```

## Boundaries and decisions

- ClipFree API binds to `127.0.0.1` by default. CORS permits the local frontend origins only. The frontend is also bound to loopback.
- ClipFree uses a new `clipfree_data` root and database, configurable with `CLIPFREE_DATA_DIR`; it does not reuse or migrate `database/codeshorts.db`.
- SQLite uses WAL mode and foreign keys. Projects and ingestion jobs persist across process restarts.
- Project IDs are UUID hex values. Server-derived project folders are resolved and constrained directly under the projects root.
- Uploads stream in 1 MiB chunks directly to an incoming file under the project source folder. No endpoint reads an entire source video into memory.
- FFprobe runs with JSON-only output, so only metadata is captured in memory. FFmpeg reads the source from disk and writes `audio/source_audio.wav` through an atomic temporary file.
- One async lock serializes FFprobe/FFmpeg ingestion work. The worker records actual upload-byte and FFmpeg `out_time_ms` telemetry when those values are available, and checks cancellation during every streaming/process loop.
- Only public direct HTTP(S) media URLs may be streamed. Each redirect is revalidated. Local/private addresses, credentialed URLs, and recognized platform URLs are rejected instead of attempting any DRM, authentication, paywall, or platform restriction bypass.
- Ollama health uses the local `/api/tags` endpoint. Highlight inference is not implemented yet.
- Transcription runs only when a compatible CTranslate2 model is local. The UI requires explicit confirmation before downloading a selected tiny/base/small/medium model; no model is pulled automatically.
- faster-whisper yields timestamped segments while CTranslate2 reads audio from disk. A process-local async lock serializes ingestion and transcription heavy work. Transcription outputs JSON/SRT/TXT and persists model, language, device, compute type, elapsed time, real-time factor, and sampled peak RAM increase.
- Auto device selection falls back to CPU/int8 and restarts the source transcription if CUDA libraries fail during inference. Explicit CUDA selection remains an actionable failure rather than silently switching devices.
- On server restart, queued/running transcription jobs become failed with a retryable explanation. One active transcription per project is enforced.
- RAM collection uses operating system APIs without a psutil dependency. On Windows, VRAM delta is sampled through NVML; unsupported/unavailable telemetry remains null and must not be interpreted as measured zero.
- Language confidence is surfaced in the transcript UI, with a prompt to choose a language and rerun when automatic detection confidence is below 0.5.
- The browser polls durable job records every five seconds. SSE/WebSocket streaming remains a later enhancement.
- Keep legacy CodeShortsBot entry points operational while ClipFree grows. Avoid broad cleanup of its outputs, generated assets, or data.

## Planned service boundaries

Later phases should add Ollama-provider, candidate-ranking, subtitle, and rendering modules. Long-running model work belongs in the bounded worker rather than a FastAPI request handler. Keep a single heavy AI inference operation active at a time and rendering concurrency explicitly bounded to fit the 6 GB GPU / 16 GB RAM target.
