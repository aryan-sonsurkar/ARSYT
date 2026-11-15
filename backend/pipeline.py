"""One-click pipeline: transcribe, find highlights, render drafts and export a ZIP.

The pipeline runs the existing local services in order as a single parent job
(kind ``pipeline``) with a real sub-job per stage, so every stage stays visible,
cancellable and retryable in the studio. Stages are idempotent: an existing
transcript, existing candidates or already rendered clips are never redone, so
a failed run can simply be started again and it resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.database import connect, now_iso, row, rows
from backend.exporting import build_zip, export_clip, safe_name
from backend.highlights import HighlightRequest, HighlightService
from backend.ingestion import IngestionError, JobCancelled
from backend.jobs import IngestionJobManager
from backend.rendering import ClipService, RenderRequest
from backend.transcription import TranscriptionRequest, TranscriptionService


class PipelineService:
    def __init__(
        self,
        jobs: IngestionJobManager,
        transcription: TranscriptionService,
        highlights: HighlightService,
        clips: ClipService,
    ) -> None:
        self.jobs = jobs
        self.transcription = transcription
        self.highlights = highlights
        self.clips = clips
        self._sub_jobs: dict[str, list[str]] = {}

    # -- public API ---------------------------------------------------------

    async def start(self, project_id: str, request: Any) -> dict:
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise IngestionError("Project not found.")
        source = Path(project.get("source_path") or "")
        if not source.is_file():
            raise IngestionError("Ingest a local source video before running the full pipeline.")
        active = row(
            "SELECT id FROM jobs WHERE project_id=? AND kind='pipeline' AND state IN ('queued','running') LIMIT 1",
            (project_id,),
        )
        if active:
            raise IngestionError("A full pipeline run is already active for this project.")
        audio_path = Path(project.get("source_path", "")).parent.parent / "audio" / "source_audio.wav"
        transcript = row("SELECT * FROM transcripts WHERE project_id=?", (project_id,))
        if transcript is None:
            model_name = (request.transcription_model or "").strip() or settings.default_whisper_model
            if not self.transcription.models.find_local_model(model_name):
                raise IngestionError(
                    f"Whisper '{model_name}' is not installed locally. Download it in Settings first."
                )
            if not audio_path.is_file():
                raise IngestionError("Extracted source audio is missing. Re-ingest the project first.")
        if not rows("SELECT * FROM candidates WHERE project_id=?", (project_id,)):
            highlight_model = (request.highlight_model or "").strip() or get_highlight_model()
            from backend.ollama import available_models
            if highlight_model not in available_models():
                raise IngestionError(
                    f"Ollama model '{highlight_model}' is not installed. Choose an installed model in Settings."
                )
        job = self.jobs.create(project_id, "pipeline")
        payload = {
            "transcription_model": (request.transcription_model or "").strip(),
            "language": request.language,
            "device": request.device,
            "highlight_model": (request.highlight_model or "").strip(),
            "count": request.count,
            "min_duration": request.min_duration,
            "max_duration": request.max_duration,
            "render": request.render.model_dump() if hasattr(request.render, "model_dump") else dict(request.render),
        }
        self.jobs.update(job["id"], payload_json=json.dumps(payload))
        self.jobs.register_task(self.run(project_id, job["id"], payload))
        return self.jobs.get(job["id"])

    async def retry(self, job_id: str) -> dict:
        previous = self.jobs.get(job_id)
        if previous.get("kind") != "pipeline":
            raise IngestionError("This is not a pipeline job.")
        try:
            payload = json.loads(previous.get("payload_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise IngestionError("The previous pipeline settings could not be recovered.") from exc
        if not payload:
            raise IngestionError("The previous pipeline job has no settings to retry.")
        return await self.start(previous["project_id"], _PipelineSettings.from_payload(payload))

    # -- orchestration ------------------------------------------------------

    async def run(self, project_id: str, job_id: str, payload: dict) -> None:
        self._sub_jobs[job_id] = []
        holder = self._sub_jobs[job_id]
        watcher = asyncio.create_task(self._watch_cancel(job_id, holder))
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        try:
            self.jobs.update(job_id, state="running", stage="starting pipeline", progress=1,
                             started_at=now_iso(), error="")
            self._fail_active_stage_jobs(project_id, job_id)

            await self._stage_transcription(project_id, job_id, payload, holder)
            await self._stage_highlights(project_id, job_id, payload, holder)
            await self._stage_approval(project_id, job_id, payload)
            rendered, failures = await self._stage_rendering(project_id, job_id, payload, holder)
            export_info = await self._stage_export(project_id, job_id, project, rendered)

            summary = f"{len(rendered)} clip(s) rendered"
            if failures:
                summary += f", {len(failures)} failed"
            if export_info:
                summary += ", ZIP export ready"
            self.jobs.update(
                job_id, state="completed", stage=summary, progress=100,
                completed_at=now_iso(), cancel_requested=0,
                payload_json=json.dumps({**payload, "result": {
                    "rendered": len(rendered), "failed": failures, "export": export_info,
                }}),
            )
        except (IngestionError, JobCancelled) as exc:
            cancelled = isinstance(exc, JobCancelled) or self.jobs.cancelled(job_id)
            for sub_id in holder:
                self._cancel_sub(sub_id)
            self.jobs.update(
                job_id,
                state="cancelled" if cancelled else "failed",
                stage="cancelled" if cancelled else "failed",
                error=str(exc), completed_at=now_iso(),
            )
            with connect() as conn:
                conn.execute(
                    "UPDATE projects SET status=?,updated_at=? WHERE id=?",
                    ("created" if cancelled else "failed", now_iso(), project_id),
                )
        except Exception as exc:
            for sub_id in holder:
                self._cancel_sub(sub_id)
            self.jobs.update(job_id, state="failed", stage="failed",
                             error=f"Pipeline failed: {exc}", completed_at=now_iso())
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            self._sub_jobs.pop(job_id, None)

    async def _stage_transcription(self, project_id: str, job_id: str, payload: dict, holder: list[str]) -> None:
        if row("SELECT project_id FROM transcripts WHERE project_id=?", (project_id,)):
            self.jobs.update(job_id, stage="transcript already exists - skipping transcription", progress=30)
            return
        model_name = (payload.get("transcription_model") or "").strip() or settings.default_whisper_model
        model_path = self.transcription.models.find_local_model(model_name)
        if not model_path:
            raise IngestionError(f"Whisper '{model_name}' is not installed locally.")
        audio_path = Path(row("SELECT source_path FROM projects WHERE id=?", (project_id,))["source_path"])
        audio_path = audio_path.parent.parent / "audio" / "source_audio.wav"
        if not audio_path.is_file():
            raise IngestionError("Extracted source audio is missing. Re-ingest the project first.")
        request = TranscriptionRequest(
            model=model_name, language=payload.get("language"),
            word_timestamps=True, device=payload.get("device") or "auto",
        )
        sub = self.jobs.create(project_id, "transcription")
        self.jobs.update(sub["id"], payload_json=json.dumps({
            "model": request.model, "language": request.language,
            "word_timestamps": True, "device": request.device,
        }))
        holder.append(sub["id"])
        self.jobs.update(job_id, stage=f"transcribing with {model_name}", progress=2)
        mirror = asyncio.create_task(self._mirror(sub["id"], job_id, 2, 32, "transcription"))
        try:
            await self.transcription.run(project_id, sub["id"], audio_path, request, model_path)
        finally:
            mirror.cancel()
            try:
                await mirror
            except asyncio.CancelledError:
                pass
        state = self.jobs.get(sub["id"])
        if state["state"] != "completed":
            raise IngestionError(f"Transcription did not finish: {state.get('error') or state['state']}")
        self.jobs.update(job_id, progress=32)

    async def _stage_highlights(self, project_id: str, job_id: str, payload: dict, holder: list[str]) -> None:
        if rows("SELECT id FROM candidates WHERE project_id=?", (project_id,)):
            self.jobs.update(job_id, stage="candidates already exist - skipping highlight detection", progress=58)
            return
        model = (payload.get("highlight_model") or "").strip() or get_highlight_model()
        request = HighlightRequest(
            model=model,
            count=int(payload.get("count") or 5),
            min_duration=float(payload.get("min_duration") or 25),
            max_duration=float(payload.get("max_duration") or 60),
        )
        sub = self.jobs.create(project_id, "highlights")
        self.jobs.update(sub["id"], payload_json=json.dumps({
            "model": request.model, "count": request.count,
            "min_duration": request.min_duration, "max_duration": request.max_duration,
        }))
        holder.append(sub["id"])
        self.jobs.update(job_id, stage=f"finding highlights with {model}", progress=33)
        mirror = asyncio.create_task(self._mirror(sub["id"], job_id, 33, 62, "highlights"))
        try:
            await self.highlights.run(project_id, sub["id"], request)
        finally:
            mirror.cancel()
            try:
                await mirror
            except asyncio.CancelledError:
                pass
        state = self.jobs.get(sub["id"])
        if state["state"] != "completed":
            raise IngestionError(f"Highlight detection did not finish: {state.get('error') or state['state']}")
        self.jobs.update(job_id, progress=62)

    async def _stage_approval(self, project_id: str, job_id: str, payload: dict) -> None:
        if rows("SELECT id FROM clips WHERE project_id=?", (project_id,)):
            self.jobs.update(job_id, stage="clips already exist - keeping current selection", progress=64)
            return
        self.jobs.update(job_id, stage="approving the best candidates", progress=63)
        candidates = self.highlights.get_candidates(project_id)
        picked = [item for item in candidates if item.get("selected")] or candidates
        limit = max(1, int(payload.get("count") or 5))
        for item in picked[:limit]:
            self.highlights.set_state(item["id"], "approved")
        self.jobs.update(job_id, progress=66)

    async def _stage_rendering(
        self, project_id: str, job_id: str, payload: dict, holder: list[str]
    ) -> tuple[list[dict], list[dict]]:
        render_request = RenderRequest.from_payload(payload.get("render") or {})
        drafts = rows(
            "SELECT * FROM clips WHERE project_id=? AND state IN ('draft','failed') ORDER BY created_at",
            (project_id,),
        )
        rendered: list[dict] = []
        failures: list[dict] = []
        total = len(drafts)
        for index, clip in enumerate(drafts):
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Pipeline cancelled by user.")
            share = 28.0 / max(1, total)
            base = 66 + share * index
            self.jobs.update(job_id, stage=f"rendering clip {index + 1}/{total}: {clip['title']}", progress=round(base, 2))
            sub = self.jobs.create(project_id, "render")
            self.jobs.update(sub["id"], payload_json=json.dumps({
                "clip_id": clip["id"], "preset": render_request.preset,
                "caption_style": render_request.caption_style,
                "caption_options": render_request.caption_options,
                "music_path": render_request.music_path, "music_volume": render_request.music_volume,
                "fps": render_request.fps, "ducking": render_request.ducking,
                "metadata_model": render_request.metadata_model, "smart_reframe": render_request.smart_reframe,
            }))
            holder.append(sub["id"])
            with connect() as conn:
                conn.execute("UPDATE clips SET state='queued',error='',updated_at=? WHERE id=?",
                             (now_iso(), clip["id"]))
            try:
                async with self.clips._render_semaphore:
                    if settings.render_concurrency <= 1:
                        async with self.jobs._heavy_operation:
                            await self.clips.run(sub["id"], clip["id"], render_request)
                    else:
                        await self.clips.run(sub["id"], clip["id"], render_request)
            finally:
                state = self.jobs.get(sub["id"])
                if state["state"] == "completed":
                    rendered.append(self.clips.get_clip(clip["id"]))
                else:
                    failures.append({"clip_id": clip["id"], "title": clip["title"],
                                     "error": state.get("error") or state["state"]})
        self.jobs.update(job_id, progress=94)
        return rendered, failures

    async def _stage_export(self, project_id: str, job_id: str, project: dict, rendered: list[dict]) -> dict:
        if not rendered:
            self.jobs.update(job_id, stage="nothing rendered - skipping export", progress=99)
            return {}
        self.jobs.update(job_id, stage="exporting clips and building the ZIP", progress=95)
        exports_dir = (settings.projects_dir / project_id / "exports").resolve()
        exports_dir.mkdir(parents=True, exist_ok=True)
        exported: list[dict] = []
        for clip in rendered:
            exported.append(await asyncio.to_thread(
                export_clip, clip, project.get("name") or "project",
            ))
        zip_path = exports_dir / f"{safe_name(project.get('name') or 'project', 'project')}-pipeline-{uuid.uuid4().hex[:6]}.zip"
        await asyncio.to_thread(
            build_zip, zip_path, [clip["id"] for clip in rendered], project.get("name") or "project",
        )
        return {"zip": str(zip_path), "filename": zip_path.name, "clip_count": len(rendered)}

    # -- helpers ------------------------------------------------------------

    async def _mirror(self, sub_id: str, parent_id: str, low: float, high: float, label: str) -> None:
        """Copy sub-job progress into the parent pipeline job."""
        while True:
            try:
                state = self.jobs.get(sub_id)
                value = low + (high - low) * float(state.get("progress") or 0) / 100
                self.jobs.update(parent_id, stage=f"{label}: {state.get('stage')}", progress=round(value, 2))
                if state["state"] in {"completed", "failed", "cancelled"}:
                    return
            except IngestionError:
                return
            await asyncio.sleep(1.0)

    async def _watch_cancel(self, parent_id: str, holder: list[str]) -> None:
        while True:
            try:
                state = self.jobs.get(parent_id)
            except IngestionError:
                return
            if state["state"] in {"completed", "failed", "cancelled"}:
                return
            if self.jobs.cancelled(parent_id):
                for sub_id in list(holder):
                    self._cancel_sub(sub_id)
                return
            await asyncio.sleep(1.0)

    def _cancel_sub(self, sub_id: str) -> None:
        try:
            state = self.jobs.get(sub_id)
            if state["state"] in {"queued", "running"}:
                self.jobs.request_cancel(sub_id)
        except IngestionError:
            pass

    def _fail_active_stage_jobs(self, project_id: str, parent_id: str) -> None:
        """Only one pipeline may drive a project's stage jobs at a time."""
        stale = rows(
            "SELECT * FROM jobs WHERE project_id=? AND kind IN ('transcription','highlights') "
            "AND state IN ('queued','running')",
            (project_id,),
        )
        for item in stale:
            self.jobs.update(
                item["id"], state="failed", stage="superseded by a new pipeline run",
                error="A newer pipeline run took over this stage.", completed_at=now_iso(),
            )


def get_highlight_model() -> str:
    from backend.database import get_setting
    return get_setting("highlight_model", settings.default_highlight_model)


class _PipelineSettings:
    """Rebuilds the request shape for a pipeline retry without pydantic."""

    def __init__(self, payload: dict) -> None:
        self.transcription_model = payload.get("transcription_model") or ""
        self.language = payload.get("language")
        self.device = payload.get("device") or "auto"
        self.highlight_model = payload.get("highlight_model") or ""
        self.count = int(payload.get("count") or 5)
        self.min_duration = float(payload.get("min_duration") or 25)
        self.max_duration = float(payload.get("max_duration") or 60)
        self.render = payload.get("render") or {}


def recover_interrupted_pipelines() -> int:
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET state='failed',stage='interrupted by restart',"
            "error='The local server restarted during the pipeline. Start it again - finished stages are skipped.',"
            "completed_at=? WHERE kind='pipeline' AND state IN ('queued','running')",
            (now_iso(),),
        )
        return cursor.rowcount
