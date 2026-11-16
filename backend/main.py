from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask

from backend.captions import CAPTION_STYLES, caption_options_from_json
from backend.config import settings
from backend.database import all_settings, connect, get_setting, init_db, now_iso, row, rows, set_setting
from backend.exporting import build_zip, export_clip, export_root, list_exports, safe_name
from backend.highlights import (
    HighlightRequest,
    HighlightService,
    recover_interrupted_highlights,
)
from backend.ingestion import (
    IngestionError,
    JobCancelled,
    probe_audio_file,
    sanitize_filename,
    stream_upload,
    validate_authorized_direct_url,
)
from backend.jobs import IngestionJobManager
from backend.metadata import generate_clip_metadata
from backend.ollama import OllamaError, available_models
from backend.pipeline import PipelineService, recover_interrupted_pipelines
from backend.publishing import PublishingService, recover_interrupted_publishes
from backend.presets import PRESETS
from backend.rendering import ClipService, RenderRequest, recover_interrupted_renders
from backend.thumbnails import create_thumbnail
from backend.social import captions_to_json, write_viral_captions
from backend.schemas import (
    BatchCandidateRequest,
    BatchClipRequest,
    BatchRenderRequest,
    CandidateUpdate,
    ClipCreateRequest,
    ClipUpdateRequest,
    HighlightStartRequest,
    JobCreate,
    PipelineStartRequest,
    ProjectCreate,
    PublishStartRequest,
    RenderStartRequest,
    SettingsUpdate,
    SocialCaptionRequest,
    SocialSaveRequest,
    TranscriptionStartRequest,
    UrlIngestRequest,
    WhisperDownloadRequest,
)
from backend.transcription import (
    TranscriptionRequest,
    TranscriptionService,
    recover_interrupted_transcriptions,
)

jobs = IngestionJobManager()
transcription = TranscriptionService(jobs)
highlights = HighlightService(jobs)
clips = ClipService(jobs)
pipeline = PipelineService(jobs, transcription, highlights, clips)
publishing = PublishingService(jobs)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    recover_interrupted_transcriptions()
    recover_interrupted_highlights()
    recover_interrupted_renders()
    recover_interrupted_pipelines()
    recover_interrupted_publishes()
    export_root().mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="ClipFree Local API",
    description="Local project, AI clipping and vertical rendering studio for ClipFree.",
    version="0.5.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def project_path(project_id: str) -> Path:
    candidate = (settings.projects_dir / project_id).resolve()
    if candidate.parent != settings.projects_dir.resolve():
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def project_or_404(project_id: str) -> dict:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def directory_size(path: Path) -> int:
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    return total


def require_free_space(expected_bytes: int = 0) -> None:
    try:
        free_bytes = shutil.disk_usage(settings.data_dir).free
    except OSError as exc:
        raise HTTPException(status_code=507, detail="ClipFree cannot inspect the local storage location.") from exc
    required = max(settings.minimum_free_bytes, expected_bytes)
    if free_bytes < required:
        raise HTTPException(
            status_code=507,
            detail="Insufficient free disk space. Free space before importing a source video.",
        )


def _safe_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if root.resolve() != resolved and root.resolve() not in resolved.parents:
        raise HTTPException(status_code=400, detail="The requested file is outside the allowed local folder.")
    return resolved


def _setting_int(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except ValueError:
        return default


def _setting_float(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except ValueError:
        return default


def _render_defaults() -> dict[str, Any]:
    return {
        "preset": get_setting("render_preset", settings.default_render_preset),
        "caption_style": get_setting("caption_style", settings.default_caption_style),
        "music_volume": _setting_float("background_music_volume", 0.18),
        "smart_reframe": get_setting("smart_reframe", "true") != "false",
        "metadata_model": get_setting("metadata_model", get_setting("highlight_model", settings.default_highlight_model)),
    }


@app.get("/api/health")
async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(f"{settings.ollama_url}/api/tags")
            response.raise_for_status()
            models = [model.get("name", "") for model in response.json().get("models", [])]
        ollama = {"available": True, "models": models, "url": settings.ollama_url}
    except (httpx.HTTPError, ValueError):
        ollama = {"available": False, "models": [], "url": settings.ollama_url}
    try:
        disk = shutil.disk_usage(settings.data_dir)
        storage = {"total_bytes": disk.total, "free_bytes": disk.free, "used_bytes": disk.total - disk.free}
    except OSError:
        storage = {"total_bytes": 0, "free_bytes": 0, "used_bytes": 0}
    return {
        "status": "ok", "ollama": ollama, "storage": storage,
        "database": str(settings.database_path), "export_dir": str(export_root()),
        "data_dir": str(settings.data_dir),
    }


@app.get("/api/settings")
async def get_settings() -> dict:
    stored = all_settings()
    try:
        models = available_models()
        ollama_online = True
    except OllamaError:
        models = []
        ollama_online = False
    return {
        "values": {
            "highlight_model": stored.get("highlight_model", settings.default_highlight_model),
            "metadata_model": stored.get("metadata_model", stored.get("highlight_model", settings.default_highlight_model)),
            "clip_count": _setting_int("clip_count", settings.default_clip_count),
            "min_clip_seconds": _setting_float("min_clip_seconds", settings.default_min_clip_seconds),
            "max_clip_seconds": _setting_float("max_clip_seconds", settings.default_max_clip_seconds),
            "render_preset": stored.get("render_preset", settings.default_render_preset),
            "caption_style": stored.get("caption_style", settings.default_caption_style),
            "background_music_volume": _setting_float("background_music_volume", 0.18),
            "smart_reframe": stored.get("smart_reframe", "true") != "false",
            "export_dir": stored.get("export_dir", str(settings.export_dir)),
            "render_concurrency": settings.render_concurrency,
            "default_whisper_model": settings.default_whisper_model,
        },
        "ollama": {"available": ollama_online, "models": models},
        "presets": [{"id": key, **value} for key, value in PRESETS.items()],
        "caption_styles": [{"id": key, "label": value["label"]} for key, value in CAPTION_STYLES.items()],
        "export_dir_resolved": str(export_root()),
    }


@app.put("/api/settings")
async def update_settings(payload: SettingsUpdate) -> dict:
    for key, value in payload.model_dump(exclude_none=True).items():
        if key == "export_dir":
            target = Path(str(value)).expanduser().resolve()
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / f".clipfree-write-{uuid.uuid4().hex}.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"The export directory is not writable: {exc}") from exc
            set_setting("export_dir", str(target))
            continue
        if key == "smart_reframe":
            set_setting(key, "true" if value else "false")
        else:
            set_setting(key, str(value))
    return await get_settings()


@app.get("/api/dashboard")
async def dashboard() -> dict:
    projects = rows("SELECT * FROM projects ORDER BY updated_at DESC")
    recent_jobs = rows(
        "SELECT jobs.*, projects.name AS project_name FROM jobs JOIN projects ON projects.id=jobs.project_id "
        "ORDER BY jobs.created_at DESC LIMIT 25"
    )
    rendered = row("SELECT COUNT(*) AS count FROM clips WHERE state='rendered'") or {"count": 0}
    completed = row("SELECT COUNT(*) AS count FROM jobs WHERE state='completed'") or {"count": 0}
    failed = row("SELECT COUNT(*) AS count FROM jobs WHERE state='failed'") or {"count": 0}
    candidates = row("SELECT COUNT(*) AS count FROM candidates") or {"count": 0}
    try:
        with connect() as conn:
            clip_rows = [dict(item) for item in conn.execute("SELECT id,title,project_id,state,output_path,duration,thumbnail_path FROM clips ORDER BY created_at DESC LIMIT 8")]
    except Exception:
        clip_rows = []
    return {
        "stats": {
            "projects": len(projects),
            "videos_processed": sum(bool(p["source_path"]) for p in projects),
            "clips_generated": int(rendered["count"]),
            "active_jobs": sum(j["state"] in ("queued", "running") for j in recent_jobs),
            "completed_jobs": int(completed["count"]),
            "failed_jobs": int(failed["count"]),
            "candidates": int(candidates["count"]),
        },
        "projects": projects[:8],
        "jobs": recent_jobs,
        "clips": [{**clip, "has_output": bool(clip.get("output_path") and Path(clip["output_path"]).is_file())} for clip in clip_rows],
        "storage_bytes": directory_size(settings.data_dir),
        "export_dir": str(export_root()),
    }


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    return rows("SELECT * FROM projects ORDER BY updated_at DESC")


@app.post("/api/projects", status_code=201)
async def create_project(payload: ProjectCreate) -> dict:
    project_id = uuid.uuid4().hex
    now = now_iso()
    path = project_path(project_id)
    for folder in ("source", "audio", "transcript", "candidates", "clips", "subtitles", "music", "exports", "metadata"):
        (path / folder).mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,source_url,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_id, payload.name, payload.source_url, now, now),
        )
    return row("SELECT * FROM projects WHERE id=?", (project_id,)) or {}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    project = project_or_404(project_id)
    project["jobs"] = rows("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC", (project_id,))
    project["transcript"] = transcription.get_transcript(project_id)
    project["candidates"] = highlights.get_candidates(project_id)
    project["clips"] = clips.list_clips(project_id)
    project["music_files"] = _music_files(project_id)
    project["settings"] = {
        "highlight_model": get_setting("highlight_model", settings.default_highlight_model),
        "clip_count": _setting_int("clip_count", settings.default_clip_count),
        "min_clip_seconds": _setting_float("min_clip_seconds", settings.default_min_clip_seconds),
        "max_clip_seconds": _setting_float("max_clip_seconds", settings.default_max_clip_seconds),
        **_render_defaults(),
    }
    return project


@app.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, confirm: bool = Query(default=False)) -> Response:
    project_or_404(project_id)
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Deleting a project removes its original source and exports. Repeat with confirm=true only after explicit user confirmation.",
        )
    path = project_path(project_id)
    with connect() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    if path.exists():
        shutil.rmtree(path)
    return Response(status_code=204)


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    return rows(
        "SELECT jobs.*, projects.name AS project_name FROM jobs JOIN projects ON projects.id=jobs.project_id "
        "ORDER BY jobs.created_at DESC"
    )


@app.post("/api/projects/{project_id}/jobs", status_code=201)
async def create_job(project_id: str, payload: JobCreate | None = None) -> dict:
    project_or_404(project_id)
    return jobs.create(project_id, (payload or JobCreate()).kind)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    result = row("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    return result


@app.post("/api/projects/{project_id}/source/upload", status_code=201)
async def upload_source(
    project_id: str,
    file: UploadFile = File(...),
    job_id: str | None = Form(default=None),
) -> dict:
    """Stream an authorized local video, then probe and extract its WAV audio."""
    project_or_404(project_id)
    try:
        filename = sanitize_filename(file.filename)
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        expected = int(file.headers.get("content-length", "0") or 0)
    except ValueError:
        expected = 0
    require_free_space(expected)
    job = jobs.create(project_id) if not job_id else jobs.get(job_id)
    if job["project_id"] != project_id:
        raise HTTPException(status_code=400, detail="That job belongs to another project")
    if job["state"] != "queued":
        raise HTTPException(status_code=409, detail="A queued ingestion job is required for this upload")
    staging = jobs.upload_staging_path(project_id, job["id"], filename)
    try:
        jobs.update(job["id"], state="running", stage="storing local upload", progress=1, started_at=now_iso(), error="")
        bytes_written = await stream_upload(
            file, staging, jobs._event(job["id"]), lambda value: jobs.progress(job["id"], value)
        )
        require_free_space(bytes_written)
        completed = await jobs.complete_uploaded_source(project_id, job["id"], staging, filename)
        completed["source_bytes_written"] = bytes_written
        return completed
    except JobCancelled as exc:
        staging.unlink(missing_ok=True)
        await jobs._finish_error(project_id, job["id"], exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IngestionError as exc:
        staging.unlink(missing_ok=True)
        await jobs._finish_error(project_id, job["id"], exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException as exc:
        staging.unlink(missing_ok=True)
        await jobs._finish_error(project_id, job["id"], IngestionError(str(exc.detail)))
        raise
    except Exception as exc:
        staging.unlink(missing_ok=True)
        await jobs._finish_error(project_id, job["id"], IngestionError(f"Local upload failed: {exc}"))
        raise HTTPException(status_code=500, detail="Local upload failed. Check available storage and try again.") from exc


@app.post("/api/projects/{project_id}/source/url", status_code=202)
async def ingest_authorized_url(project_id: str, payload: UrlIngestRequest) -> dict:
    """Queue a public direct-media URL after the caller confirms authorization."""
    if not payload.authorized:
        raise HTTPException(status_code=400, detail="Confirm authorization before importing a URL.")
    project_or_404(project_id)
    try:
        validate_authorized_direct_url(str(payload.url))
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    require_free_space()
    job = jobs.create(project_id)
    jobs.register_task(jobs.ingest_url(project_id, job["id"], str(payload.url)))
    return job


@app.get("/api/projects/{project_id}/source")
async def project_source(project_id: str) -> FileResponse:
    project = project_or_404(project_id)
    source = Path(project.get("source_path") or "")
    if not source.is_file():
        raise HTTPException(status_code=404, detail="The project source video is unavailable.")
    source = _safe_under(source, project_path(project_id) / "source")
    media_type = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo"}.get(source.suffix.lower().lstrip("."), "application/octet-stream")
    return FileResponse(source, media_type=media_type, filename=source.name)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    try:
        return jobs.request_cancel(job_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str) -> dict:
    try:
        kind = jobs.get(job_id).get("kind")
        if kind == "transcription":
            return await transcription.retry(job_id)
        if kind == "highlights":
            return await highlights.retry(job_id)
        if kind == "render":
            return await clips.retry(job_id)
        if kind == "pipeline":
            return await pipeline.retry(job_id)
        if kind == "publish":
            return await publishing.retry(job_id)
        return await jobs.retry(job_id)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/transcription/models")
async def transcription_models() -> dict:
    """List supported faster-whisper models and whether each is available locally."""
    return {
        "default_model": settings.default_whisper_model,
        "engine_available": transcription.models.engine_available(),
        "models": transcription.models.inventory(),
    }


@app.post("/api/transcription/models/{model_name}/download")
async def download_transcription_model(model_name: str, payload: WhisperDownloadRequest) -> dict:
    """Download exactly one Whisper model only after explicit caller confirmation."""
    if not payload.confirm_download:
        raise HTTPException(status_code=400, detail="Set confirm_download to true to download a Whisper model.")
    try:
        path = await transcription.models.download(model_name)
        return {"model": model_name, "installed": True, "path": str(path)}
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/pipeline", status_code=202)
async def start_pipeline(project_id: str, payload: PipelineStartRequest | None = None) -> dict:
    """One-click pipeline: transcribe, find highlights, render drafts, export a ZIP."""
    payload = payload or PipelineStartRequest()
    try:
        return await pipeline.start(project_id, payload)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/transcription", status_code=202)
async def start_transcription(project_id: str, payload: TranscriptionStartRequest) -> dict:
    try:
        request = TranscriptionRequest(
            model=payload.model, language=payload.language, word_timestamps=payload.word_timestamps, device=payload.device,
        )
        return await transcription.start(project_id, request)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/transcript")
async def get_transcript(project_id: str) -> dict:
    item = transcription.get_transcript(project_id)
    if not item:
        raise HTTPException(status_code=404, detail="No completed transcript exists for this project.")
    transcript_path = Path(item["json_path"]).resolve()
    expected_dir = (settings.projects_dir / project_id / "transcript").resolve()
    if expected_dir not in transcript_path.parents or not transcript_path.is_file():
        raise HTTPException(status_code=404, detail="Saved transcript file is unavailable.")
    try:
        content = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Saved transcript cannot be read.") from exc
    return {"metadata": item, "content": content}


@app.get("/api/projects/{project_id}/media/audio")
async def project_audio(project_id: str) -> FileResponse:
    audio_path = (settings.projects_dir / project_id / "audio" / "source_audio.wav").resolve()
    project_dir = (settings.projects_dir / project_id).resolve()
    if project_dir not in audio_path.parents or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Extracted project audio is unavailable.")
    return FileResponse(audio_path, media_type="audio/wav", filename="source_audio.wav")


# ---------------------------------------------------------------------------
# Highlight candidates
# ---------------------------------------------------------------------------


@app.post("/api/projects/{project_id}/highlights", status_code=202)
async def start_highlights(project_id: str, payload: HighlightStartRequest | None = None) -> dict:
    payload = payload or HighlightStartRequest()
    try:
        request = HighlightRequest(
            model=payload.model or get_setting("highlight_model", settings.default_highlight_model),
            count=payload.count,
            min_duration=payload.min_duration,
            max_duration=payload.max_duration,
        )
        return await highlights.start(project_id, request)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/candidates")
async def list_candidates(project_id: str) -> list[dict]:
    project_or_404(project_id)
    return highlights.get_candidates(project_id)


@app.get("/api/candidates/{candidate_id}")
async def get_candidate(candidate_id: str) -> dict:
    try:
        return highlights.get_candidate(candidate_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/candidates/{candidate_id}")
async def update_candidate(candidate_id: str, payload: CandidateUpdate) -> dict:
    try:
        return highlights.update_candidate(candidate_id, payload.model_dump(exclude_none=True))
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/candidates/batch/approve")
async def batch_approve(payload: BatchCandidateRequest) -> dict:
    approved, errors = [], []
    for candidate_id in payload.candidate_ids:
        try:
            approved.append(highlights.set_state(candidate_id, "approved"))
        except IngestionError as exc:
            errors.append({"candidate_id": candidate_id, "error": str(exc)})
    return {"approved": approved, "errors": errors}


@app.post("/api/candidates/{candidate_id}/{state}")
async def set_candidate_state(candidate_id: str, state: str) -> dict:
    if state not in {"approve", "reject", "reset"}:
        raise HTTPException(status_code=400, detail="Use approve, reject or reset.")
    target = {"approve": "approved", "reject": "rejected", "reset": "candidate"}[state]
    try:
        return highlights.set_state(candidate_id, target)
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/clips", status_code=201)
async def create_clip(project_id: str, payload: ClipCreateRequest) -> dict:
    try:
        return highlights.create_clip(project_id, payload.model_dump(exclude_none=True))
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/clips/from-approved")
async def clips_from_approved(project_id: str) -> dict:
    project_or_404(project_id)
    approved = rows("SELECT * FROM candidates WHERE project_id=? AND state='approved'", (project_id,))
    created = []
    for item in approved:
        try:
            created.append(highlights.ensure_clip(highlights.get_candidate(item["id"])))
        except IngestionError as exc:
            created.append({"candidate_id": item["id"], "error": str(exc)})
    return {"clips": created}


# ---------------------------------------------------------------------------
# Clips, rendering, captions and exports
# ---------------------------------------------------------------------------


@app.get("/api/clips")
async def list_clips(project_id: str | None = None) -> list[dict]:
    return clips.list_clips(project_id)


@app.get("/api/clips/{clip_id}")
async def get_clip(clip_id: str) -> dict:
    try:
        return clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/clips/{clip_id}")
async def update_clip(clip_id: str, payload: ClipUpdateRequest) -> dict:
    data = payload.model_dump(exclude_none=True)
    if "caption_options" in data:
        data["caption_options_json"] = data.pop("caption_options")
    try:
        return clips.update_clip(clip_id, data)
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/clips/{clip_id}", status_code=204)
async def delete_clip(clip_id: str, confirm: bool = Query(default=False)) -> Response:
    if not confirm:
        raise HTTPException(status_code=400, detail="Repeat with confirm=true to remove this clip record and its render files.")
    try:
        clips.delete_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@app.post("/api/clips/batch/render", status_code=202)
async def batch_render(payload: BatchRenderRequest) -> dict:
    started, errors = [], []
    for clip_id in payload.clip_ids:
        try:
            started.append(clips.start_render(clip_id, RenderRequest.from_payload(payload.options.model_dump())))
        except IngestionError as exc:
            errors.append({"clip_id": clip_id, "error": str(exc)})
    return {"jobs": started, "errors": errors}


@app.post("/api/clips/{clip_id}/render", status_code=202)
async def render_clip(clip_id: str, payload: RenderStartRequest | None = None) -> dict:
    payload = payload or RenderStartRequest(**_render_defaults())
    try:
        return clips.start_render(clip_id, RenderRequest.from_payload(payload.model_dump()))
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/clips/{clip_id}/video")
async def clip_video(clip_id: str) -> FileResponse:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    output = Path(clip.get("output_path") or "")
    if not output.is_file():
        raise HTTPException(status_code=404, detail="This clip has not been rendered yet.")
    return FileResponse(output, media_type="video/mp4")


@app.get("/api/clips/{clip_id}/thumbnail")
async def clip_thumbnail(clip_id: str) -> FileResponse:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    thumbnail = Path(clip.get("thumbnail_path") or "")
    if not thumbnail.is_file():
        raise HTTPException(status_code=404, detail="This clip has no thumbnail yet.")
    return FileResponse(thumbnail, media_type="image/jpeg")


@app.get("/api/clips/{clip_id}/captions")
async def clip_captions(clip_id: str) -> FileResponse:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    srt = Path(clip.get("srt_path") or "")
    if not srt.is_file():
        raise HTTPException(status_code=404, detail="This clip has no saved captions yet.")
    return FileResponse(srt, media_type="application/x-subrip", filename=f"{safe_name(clip.get('title') or 'clip')}.srt")


@app.get("/api/clips/{clip_id}/download")
async def download_clip(clip_id: str) -> FileResponse:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    output = Path(clip.get("output_path") or "")
    if not output.is_file():
        raise HTTPException(status_code=404, detail="This clip has not been rendered yet.")
    return FileResponse(output, media_type="video/mp4", filename=f"{safe_name(clip.get('title') or 'clip')}.mp4")


@app.get("/api/clips/{clip_id}/metadata")
async def clip_metadata(clip_id: str) -> dict:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return json.loads(clip.get("metadata_json") or "{}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Saved clip metadata is unreadable.") from exc


@app.post("/api/clips/{clip_id}/metadata")
async def regenerate_metadata(clip_id: str) -> dict:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    transcript = row("SELECT * FROM transcripts WHERE project_id=?", (clip["project_id"],))
    if not transcript:
        raise HTTPException(status_code=409, detail="A transcript is required to write grounded metadata.")
    try:
        content = json.loads(Path(transcript["json_path"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="The transcript file could not be read.") from exc
    excerpt = " ".join(
        str(segment.get("text", "")).strip()
        for segment in content.get("segments", [])
        if float(segment.get("end", 0)) > clip["start_time"] and float(segment.get("start", 0)) < clip["end_time"]
    )[:1800]
    model = get_setting("metadata_model", get_setting("highlight_model", settings.default_highlight_model))
    generated = await asyncio.to_thread(
        generate_clip_metadata, {"title": clip.get("title"), "category": clip.get("category")}, excerpt, model,
    )
    with connect() as conn:
        conn.execute("UPDATE clips SET metadata_json=?,updated_at=? WHERE id=?", (json.dumps(generated), now_iso(), clip_id))
    output_dir = Path(clip.get("output_path") or "").parent
    if output_dir.is_dir():
        (output_dir / "metadata.json").write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")
    # Keep the burned-in thumbnail text in sync with the new metadata hook.
    output = Path(clip.get("output_path") or "")
    if output.is_file():
        thumbnail_path = output_dir / "thumbnail.jpg"
        try:
            await asyncio.to_thread(
                create_thumbnail, output, thumbnail_path,
                str(generated.get("thumbnail_text") or clip.get("title") or "Clip"),
                min(1.2, (float(clip.get("duration") or 4.0)) / 2),
            )
        except Exception:
            pass
    return generated


@app.get("/api/publishing/status")
async def publishing_status() -> dict:
    return publishing.status()


@app.post("/api/publishing/auth/start")
async def publishing_auth_start() -> dict:
    """Start the OAuth consent flow with the user's own Google Cloud client."""
    try:
        return {"authorization_url": publishing.authorization_url()}
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/publishing/auth/callback")
async def publishing_auth_callback(state: str = Query(default=""), code: str = Query(default="")) -> Response:
    """Google redirects back here (loopback) with the consent code."""
    try:
        publishing.exchange_code(state, code)
    except IngestionError as exc:
        return Response(
            content=f"<!doctype html><title>ClipFree</title><body style='font-family:sans-serif;text-align:center;padding:4rem'><h2>Authorization failed</h2><p>{exc}</p><p>You can close this tab.</p></body>",
            media_type="text/html", status_code=400,
        )
    return Response(
        content="<!doctype html><title>ClipFree</title><body style='font-family:sans-serif;text-align:center;padding:4rem'><h2>YouTube connected</h2><p>ClipFree can now queue Shorts uploads. You can close this tab.</p></body>",
        media_type="text/html",
    )


@app.post("/api/publishing/disconnect")
async def publishing_disconnect() -> dict:
    publishing.disconnect()
    return {"authorized": False}


@app.post("/api/clips/{clip_id}/publish", status_code=202)
async def publish_clip(clip_id: str, payload: PublishStartRequest, confirm: bool = Query(default=False)) -> dict:
    """Queue an explicit, user-confirmed upload to the user's own YouTube channel."""
    try:
        return publishing.start_publish(clip_id, payload, confirm=confirm)
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/clips/{clip_id}/social")
async def clip_social(clip_id: str) -> dict:
    """Return the saved viral caption variants for a clip."""
    try:
        clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    saved = row("SELECT * FROM clip_social WHERE clip_id=?", (clip_id,))
    if not saved:
        return {"clip_id": clip_id, "variants": [], "generated_by": "", "model": "", "updated_at": ""}
    try:
        variants = json.loads(saved["variants_json"] or "[]")
    except (TypeError, ValueError):
        variants = []
    return {
        "clip_id": clip_id,
        "variants": variants,
        "generated_by": saved["generated_by"],
        "model": saved["model"],
        "updated_at": saved["updated_at"],
    }


@app.post("/api/clips/{clip_id}/social")
async def generate_clip_social(clip_id: str, payload: SocialCaptionRequest) -> dict:
    """Write new trending caption variants with the local model."""
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    transcript = row("SELECT * FROM transcripts WHERE project_id=?", (clip["project_id"],))
    if not transcript:
        raise HTTPException(status_code=409, detail="A transcript is required to write grounded captions.")
    try:
        content = json.loads(Path(transcript["json_path"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="The transcript file could not be read.") from exc
    excerpt = " ".join(
        str(segment.get("text", "")).strip()
        for segment in content.get("segments", [])
        if float(segment.get("end", 0)) > clip["start_time"] and float(segment.get("start", 0)) < clip["end_time"]
    )[:1800]
    model = payload.model or get_setting(
        "metadata_model", get_setting("highlight_model", settings.default_highlight_model)
    )
    generated = await asyncio.to_thread(
        write_viral_captions,
        {"title": clip.get("title"), "duration": clip.get("duration")},
        excerpt,
        model,
        payload.count,
    )
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO clip_social(clip_id,project_id,variants_json,model,generated_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(clip_id) DO UPDATE SET
                 variants_json=excluded.variants_json, model=excluded.model,
                 generated_by=excluded.generated_by, updated_at=excluded.updated_at""",
            (
                clip_id, clip["project_id"], json.dumps(generated["variants"], ensure_ascii=False),
                generated["model"], generated["generated_by"], now, now,
            ),
        )
    output_dir = Path(clip.get("output_path") or "").parent
    if output_dir.is_dir():
        (output_dir / "social.json").write_text(
            captions_to_json({**generated, "clip_id": clip_id, "updated_at": now}), encoding="utf-8"
        )
    return {"clip_id": clip_id, **generated, "updated_at": now}


@app.post("/api/clips/{clip_id}/social/save")
async def save_clip_social(clip_id: str, payload: SocialSaveRequest) -> dict:
    """Persist caption variants the creator edited in the studio."""
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    variants = [item.model_dump() for item in payload.variants]
    if not variants:
        raise HTTPException(status_code=422, detail="There is nothing to save.")
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO clip_social(clip_id,project_id,variants_json,model,generated_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(clip_id) DO UPDATE SET
                 variants_json=excluded.variants_json, updated_at=excluded.updated_at""",
            (
                clip_id, clip["project_id"], json.dumps(variants, ensure_ascii=False),
                get_setting("metadata_model", settings.default_highlight_model), "edited", now, now,
            ),
        )
    return {"clip_id": clip_id, "variants": variants, "generated_by": "edited", "updated_at": now}


@app.post("/api/clips/batch/export")
async def export_batch(payload: BatchClipRequest) -> dict:
    results, errors = [], []
    for clip_id in payload.clip_ids:
        try:
            clip = clips.get_clip(clip_id)
            results.append(await asyncio.to_thread(export_clip, clip, clip.get("project_name") or "project"))
        except (IngestionError, HTTPException) as exc:
            errors.append({"clip_id": clip_id, "error": str(exc)})
    return {"exports": results, "errors": errors}


@app.post("/api/clips/{clip_id}/export")
async def export_single_clip(clip_id: str) -> dict:
    try:
        clip = clips.get_clip(clip_id)
    except IngestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return await asyncio.to_thread(export_clip, clip, clip.get("project_name") or "project")
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/clips/batch/download")
async def download_batch(payload: BatchClipRequest) -> FileResponse:
    temp_dir = settings.data_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / f"clipfree-clips-{uuid.uuid4().hex}.zip"
    try:
        await asyncio.to_thread(build_zip, target, payload.clip_ids, "clipfree")
    except IngestionError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        target, media_type="application/zip", filename="clipfree-clips.zip",
        background=BackgroundTask(target.unlink, missing_ok=True),
    )


@app.post("/api/projects/{project_id}/export")
async def export_project(project_id: str) -> dict:
    project = project_or_404(project_id)
    project_clips = clips.list_clips(project_id)
    clip_ids = [item["id"] for item in project_clips if item["state"] == "rendered"]
    if not clip_ids:
        raise HTTPException(status_code=409, detail="Render at least one clip before exporting this project.")
    target = project_path(project_id) / "exports" / f"{safe_name(project['name'], 'project')}-{uuid.uuid4().hex[:6]}.zip"
    try:
        await asyncio.to_thread(build_zip, target, clip_ids, project["name"])
    except IngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"path": str(target), "filename": target.name, "clip_count": len(clip_ids), "download_url": f"/api/projects/{project_id}/export/download?name={target.name}"}


@app.get("/api/projects/{project_id}/export/download")
async def download_project_export(project_id: str, name: str) -> FileResponse:
    project_or_404(project_id)
    exports_dir = project_path(project_id) / "exports"
    target = _safe_under(exports_dir / Path(name).name, exports_dir)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="That project export is unavailable.")
    return FileResponse(target, media_type="application/zip", filename=target.name)


@app.get("/api/exports")
async def exports() -> dict:
    return {"export_dir": str(export_root()), "files": list_exports()}


@app.get("/api/exports/download")
async def download_export(path: str) -> FileResponse:
    root = export_root()
    target = _safe_under(root / path, root)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="That exported file is unavailable.")
    return FileResponse(target, filename=target.name)


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _music_files(project_id: str) -> list[dict]:
    directory = settings.projects_dir / project_id / "music"
    if not directory.is_dir():
        return []
    return [
        {"name": item.name, "size_bytes": item.stat().st_size, "path": str(item)}
        for item in sorted(directory.iterdir())
        if item.is_file() and item.suffix.lower() in MUSIC_EXTENSIONS
    ]


@app.get("/api/projects/{project_id}/music")
async def list_music(project_id: str) -> list[dict]:
    project_or_404(project_id)
    return _music_files(project_id)


@app.post("/api/projects/{project_id}/music", status_code=201)
async def upload_music(project_id: str, file: UploadFile = File(...)) -> dict:
    project_or_404(project_id)
    raw_name = Path(file.filename or "music.mp3").name
    clean_name = re.sub(r"[^A-Za-z0-9._ -]", "_", raw_name).strip(" .")[:150] or "music.mp3"
    if Path(clean_name).suffix.lower() not in MUSIC_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Use a local MP3, WAV, M4A, AAC, OGG or FLAC file.")
    music_dir = project_path(project_id) / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    target = music_dir / clean_name
    index = 1
    while target.exists():
        target = music_dir / f"{Path(clean_name).stem}_{index}{Path(clean_name).suffix}"
        index += 1
    temporary = music_dir / f".incoming-{uuid.uuid4().hex}{Path(clean_name).suffix}"
    try:
        total = 0
        with temporary.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 50 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Background music is limited to 50 MiB.")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="The music file is empty.")
        info = await asyncio.to_thread(probe_audio_file, temporary)
        temporary.replace(target)
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except IngestionError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
    return {"name": target.name, "path": str(target), **info}


@app.delete("/api/projects/{project_id}/music/{name}", status_code=204)
async def delete_music(project_id: str, name: str) -> Response:
    project_or_404(project_id)
    music_dir = project_path(project_id) / "music"
    target = _safe_under(music_dir / Path(name).name, music_dir)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="That music file is unavailable.")
    target.unlink()
    return Response(status_code=204)


@app.get("/api/events")
async def events(response: Response) -> dict:
    """Lightweight state snapshot used by the dashboard's polling fallback."""
    response.headers["Cache-Control"] = "no-store"
    return {
        "projects": rows("SELECT * FROM projects ORDER BY updated_at DESC"),
        "jobs": await list_jobs(),
        "clips": clips.list_clips()[:20],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=False)
