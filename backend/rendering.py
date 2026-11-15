"""Vertical clip rendering with smart reframing, captions, music and validation."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.boundaries import snap_boundaries
from backend.captions import CaptionOptions, caption_options_from_json, clip_srt, write_ass
from backend.config import settings
from backend.database import connect, now_iso, row, rows
from backend.ingestion import IngestionError, JobCancelled, probe_media
from backend.jobs import IngestionJobManager
from backend.media import run_ffmpeg
from backend.metadata import generate_clip_metadata
from backend.presets import preset
from backend.reframe import build_vertical_filter, detect_face_track
from backend.silence import concat_filters, plan_trim
from backend.thumbnails import create_thumbnail
RENDER_WIDTH = 1080
RENDER_HEIGHT = 1920


@dataclass
class RenderRequest:
    preset: str = "podcast"
    caption_style: str = ""
    caption_options: dict[str, Any] = field(default_factory=dict)
    music_path: str = ""
    music_volume: float = 0.18
    fps: int = 30
    ducking: bool = True
    metadata_model: str = ""
    smart_reframe: bool = True
    trim_silence: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RenderRequest":
        return cls(
            preset=str(payload.get("preset") or settings.default_render_preset),
            caption_style=str(payload.get("caption_style") or ""),
            caption_options=payload.get("caption_options") or {},
            music_path=str(payload.get("music_path") or ""),
            music_volume=max(0.0, min(1.0, float(payload.get("music_volume", 0.18) or 0.18))),
            fps=max(15, min(60, int(payload.get("fps", 30) or 30))),
            ducking=bool(payload.get("ducking", True)),
            metadata_model=str(payload.get("metadata_model") or ""),
            smart_reframe=bool(payload.get("smart_reframe", True)),
            trim_silence=bool(payload.get("trim_silence", False)),
        )


class ClipService:
    def __init__(self, jobs: IngestionJobManager) -> None:
        self.jobs = jobs
        self._render_semaphore = asyncio.Semaphore(max(1, settings.render_concurrency))

    def list_clips(self, project_id: str | None = None) -> list[dict]:
        query = (
            "SELECT clips.*, projects.name AS project_name, candidates.category AS category, "
            "candidates.overall_score AS overall_score FROM clips JOIN projects ON projects.id=clips.project_id "
            "LEFT JOIN candidates ON candidates.id=clips.candidate_id"
        )
        params: tuple[Any, ...] = ()
        if project_id:
            query += " WHERE clips.project_id=?"
            params = (project_id,)
        query += " ORDER BY clips.created_at DESC"
        items = rows(query, params)
        for item in items:
            try:
                item["caption_options"] = json.loads(item.get("caption_options_json") or "{}")
            except (TypeError, ValueError):
                item["caption_options"] = {}
            try:
                item["metadata"] = json.loads(item.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            item["has_output"] = bool(item.get("output_path") and Path(item["output_path"]).is_file())
        return items

    def get_clip(self, clip_id: str) -> dict:
        items = self.list_clips()
        for item in items:
            if item["id"] == clip_id:
                return item
        raise IngestionError("Clip not found.")

    def _transcript_segments(self, project_id: str) -> list[dict]:
        """Segments for boundary snapping; empty when no transcript is saved."""
        transcript = row("SELECT * FROM transcripts WHERE project_id=?", (project_id,))
        if not transcript:
            return []
        path = Path(transcript["json_path"])
        if not path.is_file():
            return []
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return content.get("segments") or []

    def update_clip(self, clip_id: str, payload: dict) -> dict:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            raise IngestionError("Clip not found.")
        allowed = {"title", "start_time", "end_time", "preset", "caption_style", "music_path", "music_volume", "caption_options_json"}
        updates: dict[str, Any] = {}
        for key in allowed:
            if key not in payload:
                continue
            value = payload[key]
            if key == "title":
                updates[key] = str(value or "").strip()[:140] or clip["title"]
            elif key in {"start_time", "end_time"}:
                updates[key] = max(0.0, float(value))
            elif key == "music_volume":
                updates[key] = max(0.0, min(1.0, float(value)))
            elif key == "caption_options_json":
                updates[key] = json.dumps(value) if isinstance(value, dict) else str(value)
            else:
                updates[key] = str(value)
        if "start_time" in updates or "end_time" in updates:
            start = updates.get("start_time", clip["start_time"])
            end = updates.get("end_time", clip["end_time"])
            if end - start < 3:
                raise IngestionError("A clip must be at least three seconds long.")
            segments = self._transcript_segments(clip["project_id"])
            if segments:
                start, end = snap_boundaries(segments, start, end, min_length=3.0)
            updates["start_time"], updates["end_time"] = round(start, 3), round(end, 3)
        if not updates:
            return self.get_clip(clip_id)
        updates["updated_at"] = now_iso()
        assignments = ", ".join(f"{key}=?" for key in updates)
        with connect() as conn:
            conn.execute(f"UPDATE clips SET {assignments} WHERE id=?", (*updates.values(), clip_id))
        return self.get_clip(clip_id)

    def delete_clip(self, clip_id: str) -> None:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            raise IngestionError("Clip not found.")
        with connect() as conn:
            conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        directory = Path(clip.get("output_path") or "").parent
        if directory.name == clip_id and directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)

    def start_render(self, clip_id: str, request: RenderRequest) -> dict:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            raise IngestionError("Clip not found.")
        project = row("SELECT * FROM projects WHERE id=?", (clip["project_id"],))
        if not project:
            raise IngestionError("Project not found.")
        source = Path(project.get("source_path") or "")
        if not source.is_file():
            raise IngestionError("The source video is missing. Re-ingest the project before rendering.")
        if clip["end_time"] - clip["start_time"] < 3:
            raise IngestionError("A clip must be at least three seconds long.")
        active_jobs = rows(
            "SELECT * FROM jobs WHERE project_id=? AND kind='render' AND state IN ('queued','running')",
            (clip["project_id"],),
        )
        for job in active_jobs:
            try:
                payload = json.loads(job.get("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            if payload.get("clip_id") == clip_id:
                raise IngestionError("This clip is already queued or rendering.")
        job = self.jobs.create(clip["project_id"], "render")
        payload = {
            "clip_id": clip_id, "preset": request.preset, "caption_style": request.caption_style,
            "caption_options": request.caption_options, "music_path": request.music_path,
            "music_volume": request.music_volume, "fps": request.fps, "ducking": request.ducking,
            "metadata_model": request.metadata_model, "smart_reframe": request.smart_reframe,
            "trim_silence": request.trim_silence,
        }
        self.jobs.update(job["id"], payload_json=json.dumps(payload))
        with connect() as conn:
            conn.execute("UPDATE clips SET state='queued',error='',updated_at=? WHERE id=?", (now_iso(), clip_id))

        async def run_with_slot() -> None:
            async with self._render_semaphore:
                if settings.render_concurrency <= 1:
                    # The default laptop profile serialises rendering with transcription/highlight inference.
                    async with self.jobs._heavy_operation:
                        await self.run(job["id"], clip_id, request)
                else:
                    await self.run(job["id"], clip_id, request)

        self.jobs.register_task(run_with_slot())
        return self.jobs.get(job["id"])

    async def retry(self, job_id: str) -> dict:
        previous = self.jobs.get(job_id)
        try:
            payload = json.loads(previous.get("payload_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise IngestionError("The previous render settings could not be recovered.") from exc
        clip_id = payload.get("clip_id")
        if not clip_id:
            raise IngestionError("The previous render job has no clip.")
        return self.start_render(clip_id, RenderRequest.from_payload(payload))

    async def run(self, job_id: str, clip_id: str, request: RenderRequest) -> None:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            self.jobs.update(job_id, state="failed", stage="failed", error="Clip not found.", completed_at=now_iso())
            return
        project = row("SELECT * FROM projects WHERE id=?", (clip["project_id"],))
        project_dir = (settings.projects_dir / clip["project_id"]).resolve()
        clip_dir = project_dir / "clips" / clip_id
        output = clip_dir / f"{clip_id}.mp4"
        temporary = clip_dir / f"{clip_id}.partial.mp4"
        ass_path = clip_dir / "captions.ass"
        srt_path = clip_dir / "captions.srt"
        thumbnail_path = clip_dir / "thumbnail.jpg"
        metadata_path = clip_dir / "metadata.json"
        log_path = clip_dir / "render.log"
        try:
            clip_dir.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Render cancelled by user.")
            self.jobs.update(job_id, state="running", stage="preparing clip", progress=1, started_at=now_iso(), error="")
            with connect() as conn:
                conn.execute("UPDATE clips SET state='rendering',error='',updated_at=? WHERE id=?", (now_iso(), clip_id))

            transcript = row("SELECT * FROM transcripts WHERE project_id=?", (clip["project_id"],))
            if not transcript:
                raise IngestionError("A transcript is required to generate synchronized captions.")
            transcript_path = Path(transcript["json_path"])
            if not transcript_path.is_file():
                raise IngestionError("The saved transcript is missing. Transcribe the project again.")
            content = json.loads(transcript_path.read_text(encoding="utf-8"))

            source = Path(project["source_path"])
            if not source.is_file():
                raise IngestionError("The project source video is missing.")
            metadata = json.loads(project.get("metadata_json") or "{}")
            source_info = probe_media(source)
            source_duration = float(source_info["duration_seconds"])
            start = max(0.0, min(float(clip["start_time"]), source_duration - 1))
            end = max(start + 1, min(float(clip["end_time"]), source_duration))
            if end - start < 3:
                raise IngestionError("The selected timestamps are outside the source video.")
            # Align the actual cut with a pause or complete thought so clips
            # never start mid-word or stop mid-sentence.
            start, end = snap_boundaries(content.get("segments") or [], start, end, min_length=3.0)
            end = max(start + 3.0, min(end, source_duration))
            start = min(start, end - 3.0)
            duration = end - start
            with connect() as conn:
                conn.execute(
                    "UPDATE clips SET start_time=?, end_time=?, updated_at=? WHERE id=?",
                    (round(start, 3), round(end, 3), now_iso(), clip_id),
                )

            # Opt-in dead-air removal: cut long silences and re-time captions.
            trim_plan = plan_trim(content, start, end) if request.trim_silence else None
            caption_content, caption_start, caption_end = content, start, end
            if trim_plan:
                caption_content = trim_plan["content"]
                caption_start, caption_end = 0.0, trim_plan["duration"]
                duration = trim_plan["duration"]
                self.jobs.update(job_id, stage=f"trimming {len(trim_plan['pieces']) - 1} long silence(s)")

            self.jobs.update(job_id, stage="detecting speaker framing", progress=3)
            track = []
            if request.smart_reframe:
                track = await asyncio.to_thread(detect_face_track, source, start, end)
            style_name = request.caption_style or clip.get("caption_style") or preset(request.preset)["caption_style"]
            options = caption_options_from_json(clip.get("caption_options_json"))
            if request.caption_options:
                merged = {**options.__dict__, **request.caption_options}
                options = CaptionOptions(**{key: value for key, value in merged.items() if key in CaptionOptions.__dataclass_fields__})
            framing = build_vertical_filter(
                int(source_info["video"]["width"] or 1080), int(source_info["video"]["height"] or 1920),
                RENDER_WIDTH, RENDER_HEIGHT, zoom=float(preset(request.preset)["zoom"]), track=track,
            )
            if not options.position and framing["face_detected"]:
                options.position = framing["face_side"]
            write_ass(ass_path, caption_content, caption_start, caption_end, style_name, options)
            srt_path.write_text(clip_srt(caption_content, caption_start, caption_end), encoding="utf-8")

            music_path = self._resolve_music(project_dir, request.music_path)
            self.jobs.update(job_id, stage="rendering vertical video", progress=5)
            await self._render_video(
                job_id, source, temporary, clip_dir, start, duration, framing["filter"], request,
                music_path, style_name, ass_path.name,
                pieces=trim_plan["pieces"] if trim_plan else None,
            )
            self.jobs.update(job_id, stage="validating rendered output", progress=88)
            rendered = await asyncio.to_thread(probe_media, temporary)
            if int(rendered["video"]["width"] or 0) != RENDER_WIDTH or int(rendered["video"]["height"] or 0) != RENDER_HEIGHT:
                raise IngestionError("The rendered clip does not have the expected 1080×1920 dimensions.")
            if abs(float(rendered["duration_seconds"]) - duration) > max(1.2, duration * 0.06):
                raise IngestionError("The rendered clip duration is outside the expected range.")
            if not rendered["audio"]["available"]:
                raise IngestionError("The rendered clip has no audio stream.")
            temporary.replace(output)

            excerpt = self._excerpt(content, start, end)
            metadata_model = request.metadata_model or settings.default_highlight_model
            self.jobs.update(job_id, stage=f"writing metadata with {metadata_model}", progress=91)
            generated = await asyncio.to_thread(
                generate_clip_metadata,
                {"title": clip.get("title"), "category": ""}, excerpt, metadata_model,
            )
            metadata_path.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")

            # Thumbnails burn the AI-chosen hook text, so metadata comes first.
            self.jobs.update(job_id, stage="creating thumbnail", progress=94)
            thumbnail_text = str(generated.get("thumbnail_text") or clip.get("title") or "Clip")
            try:
                await asyncio.to_thread(create_thumbnail, output, thumbnail_path, thumbnail_text, min(1.2, duration / 2))
            except Exception:
                thumbnail_path = Path("")

            size_bytes = output.stat().st_size
            with connect() as conn:
                conn.execute(
                    """UPDATE clips SET state='rendered',output_path=?,thumbnail_path=?,srt_path=?,duration=?,
                       file_size_bytes=?,metadata_json=?,preset=?,caption_style=?,caption_options_json=?,
                       music_path=?,music_volume=?,error='',updated_at=? WHERE id=?""",
                    (
                        str(output), str(thumbnail_path) if thumbnail_path else "", str(srt_path), round(rendered["duration_seconds"], 3),
                        size_bytes, json.dumps(generated), request.preset, style_name, json.dumps(options.__dict__),
                        str(music_path) if music_path else "", request.music_volume, now_iso(), clip_id,
                    ),
                )
            self.jobs.update(
                job_id, state="completed", stage=f"rendered {duration:.1f}s clip",
                progress=100, completed_at=now_iso(), cancel_requested=0,
            )
        except JobCancelled as exc:
            temporary.unlink(missing_ok=True)
            self._fail(clip_id, job_id, str(exc), cancelled=True)
        except IngestionError as exc:
            temporary.unlink(missing_ok=True)
            self._fail(clip_id, job_id, str(exc))
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            self._fail(clip_id, job_id, f"Render failed: {exc}")
        finally:
            temporary.unlink(missing_ok=True)

    async def _render_video(
        self, job_id: str, source: Path, temporary: Path, clip_dir: Path, start: float, duration: float,
        video_filter: str, request: RenderRequest, music_path: Path | None, style_name: str,
        caption_file: str, pieces: list[list[float]] | None = None,
    ) -> None:
        # ``cwd`` is the clip directory, so the caption file is passed as a
        # relative name to avoid Windows drive-letter escaping inside libass.
        caption_filter = f"ass=filename={caption_file}"
        if pieces and len(pieces) > 1:
            # Silence trimming: cut each kept piece, concat, then style.
            video_prefix, video_source = concat_filters(pieces, "0:v", "video")
            audio_prefix, audio_source = concat_filters(pieces, "0:a", "audio")
            filters = [
                f"{video_prefix};{video_source}{video_filter},fps={request.fps},{caption_filter},format=yuv420p[vout]"
            ]
            color = preset(request.preset).get("color_filter")
            if color:
                filters[0] = f"{video_prefix};{video_source}{video_filter},{color},fps={request.fps},{caption_filter},format=yuv420p[vout]"
            filters.append(
                f"{audio_prefix};{audio_source}"
                f"afade=t=in:st=0:d=0.15,afade=t=out:st={max(0, duration - 0.3):.3f}:d=0.3,"
                f"volume=1.0,aformat=sample_rates=48000:channel_layouts=stereo[speech]"
            )
        else:
            filters = [
                f"[0:v]{video_filter},fps={request.fps},{caption_filter},format=yuv420p[vout]"
            ]
            color = preset(request.preset).get("color_filter")
            if color:
                filters[0] = f"[0:v]{video_filter},{color},fps={request.fps},{caption_filter},format=yuv420p[vout]"
            filters.append(
                f"[0:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d=0.15,afade=t=out:st={max(0, duration - 0.3):.3f}:d=0.3,"
                f"volume=1.0,aformat=sample_rates=48000:channel_layouts=stereo[speech]"
            )
        arguments: list[str] = []
        if music_path:
            filters.extend([
                f"[1:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume={request.music_volume:.3f},"
                f"afade=t=in:st=0:d=0.5,afade=t=out:st={max(0, duration - 0.8):.3f}:d=0.8,"
                f"aformat=sample_rates=48000:channel_layouts=stereo[music]",
            ])
            if request.ducking:
                filters.extend([
                    "[speech]asplit=2[speech_main][speech_side]",
                    "[music][speech_side]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=350[ducked]",
                    "[speech_main][ducked]amix=inputs=2:duration=first:normalize=0[aout]",
                ])
            else:
                filters.append("[speech][music]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            filters.append("[speech]anull[aout]")

        if pieces and len(pieces) > 1:
            # With absolute trims the input must not be pre-cut.
            arguments.extend(["-i", str(source)])
        else:
            arguments.extend(["-ss", f"{float(start):.3f}", "-t", f"{duration:.3f}", "-i", str(source)])
        if music_path:
            arguments.extend(["-stream_loop", "-1", "-i", str(music_path)])
        arguments.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-t", f"{duration:.3f}", str(temporary),
        ])

        async def report(value: float) -> None:
            await self.jobs.progress(job_id, 5 + value * 0.8)

        await run_ffmpeg(
            arguments, duration, self.jobs._event(job_id), report,
            log_path=clip_dir / "render.log", cwd=clip_dir,
        )

    def _resolve_music(self, project_dir: Path, requested: str) -> Path | None:
        if not requested:
            return None
        requested_path = Path(requested)
        if requested_path.is_absolute():
            candidate = requested_path.resolve()
        else:
            candidate = (project_dir / "music" / requested_path.name).resolve()
        allowed = [(project_dir / "music").resolve(), (settings.data_dir / "music").resolve()]
        if not any(directory == candidate.parent or directory in candidate.parents for directory in allowed):
            raise IngestionError("Background music must come from this project's local music folder.")
        if not candidate.is_file():
            raise IngestionError("The selected background music file is not available locally.")
        return candidate

    @staticmethod
    def _excerpt(content: dict, start: float, end: float) -> str:
        texts = [
            str(segment.get("text", "")).strip()
            for segment in content.get("segments", [])
            if float(segment.get("end", 0)) > start and float(segment.get("start", 0)) < end
        ]
        return " ".join(text for text in texts if text)[:1800]

    def _fail(self, clip_id: str, job_id: str, error: str, cancelled: bool = False) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE clips SET state=?,error=?,updated_at=? WHERE id=?",
                ("draft" if cancelled else "failed", error, now_iso(), clip_id),
            )
        self.jobs.update(
            job_id, state="cancelled" if cancelled else "failed",
            stage="cancelled" if cancelled else "failed", error=error, completed_at=now_iso(),
        )


def recover_interrupted_renders() -> int:
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET state='failed',stage='interrupted by restart',error='The local server restarted during rendering. Retry this job.',completed_at=? "
            "WHERE kind='render' AND state IN ('queued','running')",
            (now_iso(),),
        )
        conn.execute("UPDATE clips SET state='draft',updated_at=? WHERE state IN ('queued','rendering')", (now_iso(),))
        return cursor.rowcount
