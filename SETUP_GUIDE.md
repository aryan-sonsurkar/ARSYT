# ClipFree Local Setup (Windows)

## Requirements

- Windows 11, Python 3.11 or newer, and Node.js 20 or newer.
- FFmpeg and FFprobe on PATH. Source ingestion and WAV extraction use both tools.
- Ollama is optional during Phase 1. ClipFree never downloads models automatically.

## Start the backend

From the repository root, create a virtual environment if needed and install the existing `requirements.txt` (which includes the FastAPI, Pydantic, and Uvicorn packages used by ClipFree):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8026
```

The first start creates `clipfree_data/database/clipfree.db` and local project folders. API documentation is at `http://127.0.0.1:8026/docs`.

The `requirements.txt` manifest includes faster-whisper for Phase 3 transcription. Installing the project requirements installs the engine dependency but does not download any Whisper model. If this dependency has not been installed, the backend can still start and the transcription panel will explain the prerequisite.

## Start the frontend

In another PowerShell window:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`. If the backend uses another local URL, set `NEXT_PUBLIC_API_URL` before starting Next.js.

## Ollama

The dashboard checks `http://127.0.0.1:11434/api/tags`. Override the endpoint with `CLIPFREE_OLLAMA_URL`. Ollama highlight inference is not implemented yet; no Ollama model is downloaded automatically.

## Local transcription

After installing `requirements.txt`, import a video and wait for its source job to finish. In the project's transcription panel, select tiny/base/small/medium. A Whisper model is downloaded only after the user confirms the selected model; the default is `base`. Choose Auto for language detection or enter a language code such as `hi` or `en`. The local worker saves `transcript/transcript.json`, `transcript/transcript.srt`, and `transcript/transcript.txt`.

The transcript viewer supports search and clicking a segment to seek the extracted audio. Job cancellation and retry are available in the processing activity/queue. Transcription time, real-time factor, and sampled peak RAM/VRAM increase are saved with the transcript; VRAM telemetry may be unavailable if NVML is unavailable.

Automatic language confidence is displayed in the transcript viewer. If confidence is below 50%, select the known spoken language and transcribe again. In Auto device mode, a CUDA runtime/library failure during inference retries on CPU/int8; explicitly selecting CUDA reports the GPU error instead of silently switching devices.

## Local data and isolation

Projects and job metadata live in `clipfree_data`. Set `CLIPFREE_DATA_DIR` before starting the API to choose a different local data directory. The existing CodeShortsBot database, outputs and configuration are kept separate.

## Import a source video

Create a project in the browser, then choose a local `.mp4`, `.m4v`, `.mov`, `.mkv`, `.webm`, or `.avi` file. ClipFree streams the source into that project's `source` folder, reads actual media data using FFprobe, and writes `audio/source_audio.wav` as 16 kHz mono PCM.

You can also provide a public direct media URL after confirming you are authorized to process it. ClipFree intentionally rejects platform pages, authenticated URLs, paywalled sources, private-network destinations, and DRM-protected content. Download an authorized local copy through the source's permitted method, then upload that file.

Default limits are 20 GiB per source and 4 GiB free space before an import. Override them only when the storage location can safely hold the source and working WAV:

```powershell
$env:CLIPFREE_MAX_UPLOAD_BYTES = 21474836480
$env:CLIPFREE_MIN_FREE_BYTES = 4294967296
```
