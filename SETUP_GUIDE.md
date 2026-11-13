# ClipFree Local Setup (Windows)

ClipFree turns an authorized local podcast/interview video into vertical Shorts entirely on this machine. Nothing is uploaded to a paid service and nothing is auto-published.

## Requirements

- Windows 11, Python 3.11+ (validated with 3.11.9), Node.js 20+.
- FFmpeg and FFprobe on `PATH`.
- Ollama running locally at `http://127.0.0.1:11434` with `gemma3:4b` pulled.
- About 6 GB VRAM / 16 GB RAM recommended. CPU-only operation works but is slower.
- Local `faster-whisper` and a Whisper model. ClipFree never downloads a model without confirmation.

## Install

```powershell
cd "C:\path\to\codeshortsbot"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` now includes FastAPI, Uvicorn, faster-whisper, Pillow, NumPy and `opencv-python-headless`. The headless OpenCV build avoids a GUI dependency; OpenCV 5 removed Haar cascades, so reframing falls back to a centre crop (documented in `KNOWN_ISSUES.md`).

Frontend dependencies:

```powershell
cd frontend
npm install
```

## Pull the local models (once)

```powershell
ollama pull gemma3:4b
```

Transcription models are managed inside ClipFree. The validated model on this laptop is `small` (CPU/int8); it is already installed. If you need another size, open a project and use the transcription model panel — ClipFree asks for confirmation before downloading.

## Start the services

Terminal 1 (backend):

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8026
```

You can also run `.\.venv\Scripts\python.exe -m backend.main`.

Terminal 2 (frontend):

```powershell
cd frontend
npm run dev
```

Open `http://localhost:3000`. API docs: `http://127.0.0.1:8026/docs`.

For a production frontend build:

```powershell
cd frontend
npm run build
npm run start
```

## Workflow

1. **Create a project** and upload an authorized local `.mp4`, `.m4v`, `.mov`, `.mkv`, `.webm` or `.avi` file, or a public direct media URL you are allowed to process.
2. **Wait for ingestion** — FFprobe metadata, then a 16 kHz mono WAV in `audio/`.
3. **Transcribe** — choose `small` (default) or another installed model, Auto language (recommended) and VAD on. Outputs land in `transcript/`.
4. **Find highlights** — ClipFree chunks the transcript and asks local `gemma3:4b` for candidates. Review, edit, approve/reject, then generate clips from approved candidates.
5. **Render** — pick a preset and caption style, optionally add local music, and render. Captions are burned in; output is 1080×1920 H.264/AAC.
6. **Finish** — generate metadata/thumbnail, preview, download a single clip, batch-export, or export the whole project as a ZIP.

All results persist in `clipfree_data/` and survive backend restarts.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLIPFREE_DATA_DIR` | `./clipfree_data` | Data root (DB, projects, models, exports) |
| `CLIPFREE_HOST` / `CLIPFREE_PORT` | `127.0.0.1` / `8026` | API binding |
| `CLIPFREE_OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint |
| `CLIPFREE_OLLAMA_TIMEOUT` | `180` | Highlight request timeout (seconds) |
| `CLIPFREE_HIGHLIGHT_MODEL` | `gemma3:4b` | Highlight/metadata model |
| `CLIPFREE_WHISPER_MODEL` | `small` | Default transcription model |
| `CLIPFREE_CLIP_COUNT` | `5` | Suggested clips per run |
| `CLIPFREE_MIN_CLIP_SECONDS` / `MAX` | `25` / `60` | Candidate duration bounds |
| `CLIPFREE_RENDER_PRESET` | `podcast` | Default editing preset |
| `CLIPFREE_CAPTION_STYLE` | `kinetic` | Default caption style |
| `CLIPFREE_RENDER_CONCURRENCY` | `1` | Parallel renders (keep 1 on 6 GB VRAM) |
| `CLIPFREE_MAX_UPLOAD_BYTES` | 20 GiB | Per-source upload limit |
| `CLIPFREE_MIN_FREE_BYTES` | 4 GiB | Required free space before import |
| `CLIPFREE_EXPORT_DIR` | `./clipfree_data/exports` | Export destination |

## Local transcription notes

- Default is `small` on CPU/int8. `test.wav` and a 28.4 s narration sample validated English detection at 99.4% and 99.9%.
- A 6 m 16 s English podcast transcribes in roughly 54 s (RTF ≈ 0.14) on this laptop.
- Auto device falls back to CPU/int8 if CUDA libraries are incomplete; explicitly selecting CUDA reports the real error.
- Keep VAD on. With VAD off, a trailing hallucinated sentence can appear after silence.

## Rendering notes

- Renders are serialized by default. A 20–25 s clip takes well under a minute on CPU for this source, but complex sources and higher presets vary.
- Captions are burned into the MP4; the SRT sidecar is still exported.
- Face tracking is best-effort. OpenCV 5 has no Haar cascade support, so ClipFree uses a safe centre crop instead of failing.

## Data and privacy

- `clipfree_data/` is local and Git-ignored.
- `.env` is Git-ignored; `.env.example` is committed.
- ClipFree refuses DRM, credentialed, paywalled, private-network and platform-restriction bypasses.
- No automatic social publishing.

## Troubleshooting

- **Port in use:** stop the previous process or set `CLIPFREE_PORT`.
- **Ollama unavailable:** confirm `ollama list` shows `gemma3:4b` and that `http://127.0.0.1:11434/api/tags` responds.
- **No Whisper model:** open the transcription panel and confirm a model download, or reuse an installed model directory.
- **CUDA errors:** leave device on Auto; CPU/int8 is the validated path on this laptop.
- **No captions:** re-render the clip; captions are generated and burned during render, not retroactively.
- **Stale UI:** the UI polls every 5 seconds; refresh after a backend restart.
- **Tests:** `.\.venv\Scripts\python.exe -m pytest -q tests/test_clipfree_ingestion.py tests/test_clipfree_transcription.py tests/test_clipfree_studio.py`.
- **Full E2E:** `.\.venv\Scripts\python.exe tests/e2e_workflow.py` (requires the generated `temp/e2e_podcast.mp4` and a running backend + Ollama).
