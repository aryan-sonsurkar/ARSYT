"""Durable, single-worker ingestion jobs with cancellation and retry support."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from backend.config import settings
from backend.database import connect, now_iso, row
from backend.ingestion import (
    IngestionError,
    JobCancelled,
    _assert_safe_path,
    _validate_container_signature,
    extract_wav_audio,
    probe_media,
    sanitize_filename,
    stream_direct_url,
)


class IngestionJobManager:
    """Runs one FFmpeg ingestion pipeline at a time to protect local resources."""

    def __init__(self) -> None:
        self._heavy_operation = asyncio.Lock()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()

    def create(self, project_id: str, kind: str = "ingestion") -> dict:
        job_id = uuid.uuid4().hex
        now = now_iso()
        with connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, project_id, kind, "queued", "queued", 0, now),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        result = row("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not result:
            raise IngestionError("Job not found.")
        return result

    def _event(self, job_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(job_id, asyncio.Event())

    def cancelled(self, job_id: str) -> bool:
        record = self.get(job_id)
        return self._event(job_id).is_set() or bool(record.get("cancel_requested"))

    def update(self, job_id: str, **values: object) -> dict:
        allowed = {"state", "stage", "progress", "error", "started_at", "completed_at", "cancel_requested", "retry_count", "payload_json"}
        changes = {key: value for key, value in values.items() if key in allowed}
        if not changes:
            return self.get(job_id)
        assignments = ", ".join(f"{key}=?" for key in changes)
        with connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*changes.values(), job_id))
        return self.get(job_id)

    def request_cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        self._event(job_id).set()
        if job["state"] == "queued":
            return self.update(
                job_id, state="cancelled", stage="cancelled", error="Job cancelled by user.",
                completed_at=now_iso(), cancel_requested=1,
            )
        return self.update(job_id, cancel_requested=1, stage="cancelling")

    async def progress(self, job_id: str, value: float) -> None:
        if self.cancelled(job_id):
            raise JobCancelled("Job cancelled by user.")
        self.update(job_id, progress=round(max(0, min(100, value)), 2))

    def _project(self, project_id: str) -> dict:
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise IngestionError("Project not found.")
        return project

    def _project_dir(self, project_id: str) -> Path:
        directory = (settings.projects_dir / project_id).resolve()
        if directory.parent != settings.projects_dir.resolve():
            raise IngestionError("Invalid project ID.")
        return directory

    def upload_staging_path(self, project_id: str, job_id: str, filename: str) -> Path:
        source_dir = self._project_dir(project_id) / "source"
        safe_name = sanitize_filename(filename)
        return source_dir / f".incoming-{job_id}-{safe_name}"

    def register_task(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def complete_uploaded_source(self, project_id: str, job_id: str, staging: Path, filename: str) -> dict:
        return await self._adopt_and_process(project_id, job_id, staging, filename, source_url="")

    async def ingest_url(self, project_id: str, job_id: str, source_url: str) -> None:
        staging = self.upload_staging_path(project_id, job_id, "download.mp4")
        try:
            await self._start(job_id, "downloading authorized source")
            downloaded_bytes, filename = await stream_direct_url(
                source_url, staging, self._event(job_id), lambda value: self.progress(job_id, value)
            )
            if downloaded_bytes < 1:
                raise IngestionError("The URL did not return any bytes.")
            await self._adopt_and_process(project_id, job_id, staging, filename, source_url=source_url)
        except (IngestionError, JobCancelled) as exc:
            staging.unlink(missing_ok=True)
            await self._finish_error(project_id, job_id, exc)
        except Exception as exc:
            staging.unlink(missing_ok=True)
            await self._finish_error(project_id, job_id, IngestionError(f"URL ingestion failed: {exc}"))

    async def process_existing_source(self, project_id: str, job_id: str, source_path: Path) -> None:
        try:
            project_dir = self._project_dir(project_id)
            _assert_safe_path(source_path, project_dir / "source")
            if not source_path.is_file():
                raise IngestionError("The original local source is no longer available. Upload it again to retry.")
            await self._start(job_id, "validating source")
            async with self._heavy_operation:
                await self._process_source(project_id, job_id, source_path, source_url=None, adopt=False)
        except (IngestionError, JobCancelled) as exc:
            await self._finish_error(project_id, job_id, exc)
        except Exception as exc:
            await self._finish_error(project_id, job_id, IngestionError(f"Source processing failed: {exc}"))

    async def retry(self, job_id: str) -> dict:
        previous = self.get(job_id)
        project = self._project(previous["project_id"])
        source_path = Path(project.get("source_path", ""))
        if not source_path.is_file():
            raise IngestionError("Retry is available after a valid local source has been saved. Re-upload or provide a direct authorized URL.")
        retry = self.create(previous["project_id"], previous.get("kind", "ingestion"))
        self.update(retry["id"], retry_count=int(previous.get("retry_count", 0)) + 1)
        self.register_task(self.process_existing_source(previous["project_id"], retry["id"], source_path))
        return self.get(retry["id"])

    async def _adopt_and_process(
        self, project_id: str, job_id: str, staging: Path, filename: str, source_url: str
    ) -> dict:
        try:
            await self._start(job_id, "validating source")
            async with self._heavy_operation:
                return await self._process_source(project_id, job_id, staging, source_url=source_url, adopt=True, filename=filename)
        except (IngestionError, JobCancelled) as exc:
            staging.unlink(missing_ok=True)
            await self._finish_error(project_id, job_id, exc)
            raise
        except Exception as exc:
            staging.unlink(missing_ok=True)
            wrapped = IngestionError(f"Source processing failed: {exc}")
            await self._finish_error(project_id, job_id, wrapped)
            raise wrapped from exc

    async def _start(self, job_id: str, stage: str) -> None:
        if self.cancelled(job_id):
            raise JobCancelled("Job cancelled by user.")
        self.update(job_id, state="running", stage=stage, progress=max(self.get(job_id)["progress"], 1), started_at=now_iso(), error="")

    async def _process_source(
        self,
        project_id: str,
        job_id: str,
        source_path: Path,
        source_url: str | None,
        adopt: bool,
        filename: str | None = None,
    ) -> dict:
        project_dir = self._project_dir(project_id)
        source_dir = project_dir / "source"
        _assert_safe_path(source_path, source_dir)
        if self.cancelled(job_id):
            raise JobCancelled("Job cancelled by user.")
        await asyncio.to_thread(_validate_container_signature, source_path)
        self.update(job_id, stage="extracting media metadata", progress=60)
        metadata = await asyncio.to_thread(probe_media, source_path)
        await self.progress(job_id, 65)

        if adopt:
            safe_filename = sanitize_filename(filename)
            final_path = _unique_source_path(source_dir, safe_filename)
            source_path.replace(final_path)
            source_path = final_path
        if source_url is not None:
            with connect() as conn:
                conn.execute(
                    """UPDATE projects SET source_url=?,source_path=?,source_title=?,source_filename=?,source_size_bytes=?,metadata_json=?,status=?,updated_at=? WHERE id=?""",
                    (
                        source_url, str(source_path), source_path.stem, source_path.name,
                        metadata["file_size_bytes"], json.dumps(metadata), "processing_source", now_iso(), project_id,
                    ),
                )

        audio_path = project_dir / "audio" / "source_audio.wav"
        self.update(job_id, stage="extracting 16 kHz mono audio", progress=65)
        audio_info = await extract_wav_audio(
            source_path, audio_path, metadata["duration_seconds"], self._event(job_id), lambda value: self.progress(job_id, value)
        )
        metadata["extracted_audio"] = audio_info
        with connect() as conn:
            conn.execute(
                "UPDATE projects SET source_path=?,source_title=?,source_filename=?,source_size_bytes=?,metadata_json=?,status=?,updated_at=? WHERE id=?",
                (str(source_path), source_path.stem, source_path.name, metadata["file_size_bytes"], json.dumps(metadata), "source_ready", now_iso(), project_id),
            )
        self.update(job_id, state="completed", stage="source ready", progress=100, completed_at=now_iso(), cancel_requested=0)
        return self.get(job_id)

    async def _finish_error(self, project_id: str, job_id: str, error: Exception) -> None:
        cancelled = isinstance(error, JobCancelled) or self.cancelled(job_id)
        self.update(
            job_id,
            state="cancelled" if cancelled else "failed",
            stage="cancelled" if cancelled else "failed",
            error=str(error),
            completed_at=now_iso(),
            progress=self.get(job_id)["progress"],
        )
        with connect() as conn:
            conn.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", ("created" if cancelled else "failed", now_iso(), project_id))


def _unique_source_path(directory: Path, filename: str) -> Path:
    base = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = directory / filename
    index = 1
    while candidate.exists():
        candidate = directory / f"{base}_{index}{suffix}"
        index += 1
    return candidate
