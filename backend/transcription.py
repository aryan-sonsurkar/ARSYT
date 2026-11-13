"""Local faster-whisper transcription with durable results and bounded resources."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.database import connect, now_iso, row
from backend.ingestion import IngestionError, JobCancelled, probe_audio
from backend.jobs import IngestionJobManager

MODEL_CATALOG: dict[str, dict[str, str]] = {
    "tiny": {"size": "~75 MB", "memory": "~1 GB RAM / low VRAM", "description": "Fastest, lowest accuracy."},
    "base": {"size": "~145 MB", "memory": "~1.5 GB RAM / ~1 GB VRAM", "description": "Default for this laptop; balanced speed and accuracy."},
    "small": {"size": "~466 MB", "memory": "~3 GB RAM / ~2 GB VRAM", "description": "Higher accuracy for clearer speech."},
    "medium": {"size": "~1.5 GB", "memory": "~6 GB RAM / may exceed practical 6 GB VRAM", "description": "Higher accuracy; CPU fallback is safer on this device."},
}


def _process_memory_bytes() -> int | None:
    """Return resident/peak process memory without requiring an optional package."""
    if os.name == "nt":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("Psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize)
        return None

    try:
        import resource

        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError):
        return None
    return int(peak_rss if sys.platform == "darwin" else peak_rss * 1024)


def _gpu_memory_bytes() -> int | None:
    """Return device-wide NVIDIA VRAM use through NVML when the driver exposes it."""
    if os.name != "nt":
        return None

    class NvmlMemoryInfo(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong),
            ("used", ctypes.c_ulonglong),
        ]

    try:
        nvml = ctypes.WinDLL("nvml.dll", use_last_error=True)
    except OSError:
        return None
    nvml.nvmlInit_v2.restype = ctypes.c_int
    if nvml.nvmlInit_v2() != 0:
        return None
    try:
        handle = ctypes.c_void_p()
        info = NvmlMemoryInfo()
        nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        nvml.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(NvmlMemoryInfo)]
        nvml.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            return None
        if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(info)) != 0:
            return None
        return int(info.used)
    finally:
        nvml.nvmlShutdown()


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def transcript_to_srt(segments: list[dict[str, Any]]) -> str:
    entries: list[str] = []
    for index, segment in enumerate(segments, start=1):
        entries.append(
            f"{index}\n{format_srt_timestamp(segment['start'])} --> {format_srt_timestamp(segment['end'])}\n{segment['text'].strip()}\n"
        )
    return "\n".join(entries)


class WhisperModelManager:
    """Discovers local CTranslate2 models and downloads only on explicit API confirmation."""

    @staticmethod
    def engine_available() -> bool:
        try:
            from importlib.util import find_spec

            return find_spec("faster_whisper") is not None
        except (ImportError, ValueError):
            return False

    def _is_model_dir(self, path: Path) -> bool:
        return path.is_dir() and (path / "model.bin").is_file() and (path / "config.json").is_file()

    def find_local_model(self, name: str) -> Path | None:
        if name not in MODEL_CATALOG:
            return None
        direct = settings.whisper_models_dir / name
        if self._is_model_dir(direct):
            return direct
        cache_root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
        model_root = cache_root / f"models--Systran--faster-whisper-{name}" / "snapshots"
        if model_root.is_dir():
            for snapshot in sorted(model_root.iterdir(), reverse=True):
                if self._is_model_dir(snapshot):
                    return snapshot
        return None

    def inventory(self) -> list[dict[str, Any]]:
        try:
            import ctranslate2
            cuda_available = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda_available = False
        return [
            {
                "name": name,
                **details,
                "installed": self.find_local_model(name) is not None,
                "path": str(self.find_local_model(name)) if self.find_local_model(name) else "",
                "recommended_device": "cuda" if cuda_available and name in {"tiny", "base", "small"} else "cpu",
                "recommended_compute_type": "int8_float16" if cuda_available and name in {"tiny", "base", "small"} else "int8",
            }
            for name, details in MODEL_CATALOG.items()
        ]

    async def download(self, name: str) -> Path:
        if name not in MODEL_CATALOG:
            raise IngestionError("Choose tiny, base, small, or medium Whisper.")
        if not self.engine_available():
            raise IngestionError("The faster-whisper dependency is not installed. Install the project requirements before downloading a model.")
        existing = self.find_local_model(name)
        if existing:
            return existing
        target = settings.whisper_models_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            from faster_whisper import download_model
            downloaded = await asyncio.to_thread(download_model, name, output_dir=str(target), local_files_only=False)
        except Exception as exc:
            raise IngestionError(f"Whisper model download failed: {exc}") from exc
        path = Path(downloaded)
        if not self._is_model_dir(path):
            raise IngestionError("The downloaded Whisper model is incomplete.")
        return path


@dataclass(frozen=True)
class TranscriptionRequest:
    model: str
    language: str | None
    word_timestamps: bool
    device: str


class TranscriptionService:
    def __init__(self, jobs: IngestionJobManager) -> None:
        self.jobs = jobs
        self.models = WhisperModelManager()

    def get_transcript(self, project_id: str) -> dict[str, Any] | None:
        return row("SELECT * FROM transcripts WHERE project_id=?", (project_id,))

    async def start(self, project_id: str, request: TranscriptionRequest) -> dict:
        if not self.models.engine_available():
            raise IngestionError("The faster-whisper dependency is not installed. Install the project requirements before starting transcription.")
        model_path = self.models.find_local_model(request.model)
        if not model_path:
            raise IngestionError(
                f"Whisper '{request.model}' is not installed locally. Download it explicitly from Settings or place a compatible CTranslate2 model in {settings.whisper_models_dir}."
            )
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise IngestionError("Project not found.")
        audio_path = Path(project.get("source_path", "")).parent.parent / "audio" / "source_audio.wav"
        if not audio_path.is_file():
            raise IngestionError("Extracted source audio is missing. Re-run source ingestion first.")
        active_job = row(
            "SELECT id FROM jobs WHERE project_id=? AND kind='transcription' AND state IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        if active_job:
            raise IngestionError("A transcription job is already active for this project.")
        job = self.jobs.create(project_id, "transcription")
        payload = {"model": request.model, "language": request.language, "word_timestamps": request.word_timestamps, "device": request.device}
        self.jobs.update(job["id"], payload_json=json.dumps(payload))
        self.jobs.register_task(self.run(project_id, job["id"], audio_path, request, model_path))
        return self.jobs.get(job["id"])

    async def retry(self, job_id: str) -> dict:
        old_job = self.jobs.get(job_id)
        if old_job.get("kind") != "transcription":
            raise IngestionError("This is not a transcription job.")
        try:
            payload = json.loads(old_job.get("payload_json") or "{}")
            request = TranscriptionRequest(**payload)
        except (TypeError, ValueError) as exc:
            raise IngestionError("The previous transcription settings could not be recovered.") from exc
        return await self.start(old_job["project_id"], request)

    async def run(
        self, project_id: str, job_id: str, audio_path: Path, request: TranscriptionRequest, model_path: Path
    ) -> None:
        ram_before: int | None = None
        vram_before: int | None = None
        ram_samples: list[int] = []
        vram_samples: list[int] = []
        started = time.perf_counter()
        transcript_dir = (settings.projects_dir / project_id / "transcript").resolve()
        partial_path = transcript_dir / "transcript.partial.json"
        try:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Transcription cancelled by user.")
            self.jobs.update(job_id, state="running", stage="loading local Whisper model", progress=0.0, started_at=now_iso(), error="")
            audio = await asyncio.to_thread(probe_audio, audio_path)
            duration = audio["duration_seconds"]
            async with self.jobs._heavy_operation:
                if self.jobs.cancelled(job_id):
                    raise JobCancelled("Transcription cancelled by user.")
                device, compute_type = self._device_options(request.device)
                self.jobs.update(job_id, stage=f"transcribing with {request.model} on {device}", progress=0.0)
                ram_before = _process_memory_bytes()
                vram_before = _gpu_memory_bytes()
                if ram_before is not None:
                    ram_samples.append(ram_before)
                if vram_before is not None:
                    vram_samples.append(vram_before)
                monitor = asyncio.create_task(self._monitor_resources(ram_samples, vram_samples))
                try:
                    result, device, compute_type = await asyncio.to_thread(
                        self._transcribe_blocking, model_path, audio_path, request, device, compute_type, duration, job_id
                    )
                finally:
                    monitor.cancel()
                    try:
                        await monitor
                    except asyncio.CancelledError:
                        pass
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Transcription cancelled by user.")
            elapsed = time.perf_counter() - started
            paths = self._write_outputs(transcript_dir, result)
            ram_peak = max(ram_samples) if ram_samples else None
            vram_peak = max(vram_samples) if vram_samples else None
            self._save_transcript(
                project_id, job_id, request, result, paths, elapsed, ram_before, ram_peak,
                vram_before, vram_peak, device, compute_type,
            )
            self.jobs.update(job_id, state="completed", stage="transcript ready", progress=100, completed_at=now_iso(), cancel_requested=0)
            partial_path.unlink(missing_ok=True)
        except (IngestionError, JobCancelled) as exc:
            self.jobs.update(
                job_id, state="cancelled" if isinstance(exc, JobCancelled) else "failed",
                stage="cancelled" if isinstance(exc, JobCancelled) else "failed", error=str(exc), completed_at=now_iso(),
            )
        except Exception as exc:
            self.jobs.update(job_id, state="failed", stage="failed", error=f"Transcription failed: {exc}", completed_at=now_iso())

    async def _monitor_resources(self, ram_samples: list[int], vram_samples: list[int]) -> None:
        while True:
            ram_usage = await asyncio.to_thread(_process_memory_bytes)
            vram_usage = await asyncio.to_thread(_gpu_memory_bytes)
            if ram_usage is not None:
                ram_samples.append(ram_usage)
            if vram_usage is not None:
                vram_samples.append(vram_usage)
            await asyncio.sleep(0.5)

    def _device_options(self, preferred: str) -> tuple[str, str]:
        try:
            import ctranslate2
            has_cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            has_cuda = False
        if preferred == "cuda" and not has_cuda:
            raise IngestionError("CUDA transcription was requested but CTranslate2 cannot access an NVIDIA GPU.")
        if preferred != "cpu" and has_cuda:
            return "cuda", "int8_float16"
        return "cpu", "int8"

    def _transcribe_blocking(
        self, model_path: Path, audio_path: Path, request: TranscriptionRequest,
        device: str, compute_type: str, duration: float, job_id: str,
    ) -> tuple[dict[str, Any], str, str]:
        """faster-whisper yields segments progressively; audio itself is streamed by CTranslate2."""
        try:
            from faster_whisper import WhisperModel
            def transcribe_with(model: Any) -> dict[str, Any]:
                segments_source, info = model.transcribe(
                    str(audio_path), language=request.language, word_timestamps=request.word_timestamps,
                    vad_filter=True, beam_size=5, condition_on_previous_text=False,
                )
                segments: list[dict[str, Any]] = []
                for segment in segments_source:
                    if self.jobs.cancelled(job_id):
                        raise JobCancelled("Transcription cancelled by user.")
                    words = [
                        {"start": round(word.start, 3), "end": round(word.end, 3), "word": word.word, "probability": round(word.probability, 4)}
                        for word in (segment.words or [])
                    ]
                    segments.append({
                        "start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text.strip(),
                        "avg_logprob": round(segment.avg_logprob, 4), "no_speech_prob": round(segment.no_speech_prob, 4), "words": words,
                    })
                    self.jobs.update(job_id, stage="transcribing audio", progress=round(min(99.0, (segment.end / max(duration, 0.001)) * 99.0), 2))
                return {
                    "language": info.language or request.language or "",
                    "language_probability": round(float(getattr(info, "language_probability", 0) or 0), 4),
                    "duration_seconds": duration, "segments": segments,
                }

            model = WhisperModel(
                str(model_path), device=device, compute_type=compute_type,
                cpu_threads=4, num_workers=1, local_files_only=True,
            )
            try:
                result = transcribe_with(model)
            except Exception as exc:
                message = str(exc).lower()
                missing_cuda_runtime = any(
                    marker in message
                    for marker in ("cublas", "cudnn", "cuda runtime", "could not load library", "cannot be loaded")
                )
                if request.device != "auto" or device != "cuda" or not missing_cuda_runtime:
                    raise
                device, compute_type = self._device_options("cpu")
                self.jobs.update(
                    job_id,
                    stage="CUDA libraries unavailable; restarting transcription on CPU",
                    progress=0,
                )
                model = WhisperModel(
                    str(model_path), device=device, compute_type=compute_type,
                    cpu_threads=4, num_workers=1, local_files_only=True,
                )
                try:
                    result = transcribe_with(model)
                except Exception as cpu_exc:
                    raise IngestionError(
                        f"CUDA inference failed ({exc}); CPU fallback also failed: {cpu_exc}"
                    ) from cpu_exc
            return {
                **result,
            }, device, compute_type
        except JobCancelled:
            raise
        except Exception as exc:
            raise IngestionError(f"faster-whisper could not transcribe this audio: {exc}") from exc

    def _write_outputs(self, directory: Path, result: dict[str, Any]) -> dict[str, Path]:
        json_path = directory / "transcript.json"
        srt_path = directory / "transcript.srt"
        text_path = directory / "transcript.txt"
        temporary_json = directory / "transcript.partial.json"
        temporary_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_json.replace(json_path)
        srt_path.write_text(transcript_to_srt(result["segments"]), encoding="utf-8")
        text_path.write_text("\n".join(segment["text"] for segment in result["segments"] if segment["text"]), encoding="utf-8")
        return {"json": json_path, "srt": srt_path, "text": text_path}

    def _save_transcript(
        self, project_id: str, job_id: str, request: TranscriptionRequest, result: dict[str, Any], paths: dict[str, Path],
        elapsed: float, ram_before: int | None, ram_peak: int | None,
        vram_before: int | None, vram_peak: int | None, device: str, compute_type: str,
    ) -> None:
        segments = result["segments"]
        words = sum(len(segment["words"]) for segment in segments)
        duration = result["duration_seconds"]
        ram_delta = (
            max(0, ram_peak - ram_before)
            if ram_before is not None and ram_peak is not None
            else 0
        )
        vram_delta = (
            max(0, vram_peak - vram_before)
            if vram_before is not None and vram_peak is not None
            else None
        )
        now = now_iso()
        with connect() as conn:
            conn.execute(
                """INSERT INTO transcripts(project_id,job_id,model_name,language,language_probability,device,compute_type,json_path,srt_path,text_path,duration_seconds,segment_count,word_count,elapsed_seconds,real_time_factor,ram_delta_bytes,vram_delta_bytes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET job_id=excluded.job_id,model_name=excluded.model_name,language=excluded.language,language_probability=excluded.language_probability,device=excluded.device,compute_type=excluded.compute_type,json_path=excluded.json_path,srt_path=excluded.srt_path,text_path=excluded.text_path,duration_seconds=excluded.duration_seconds,segment_count=excluded.segment_count,word_count=excluded.word_count,elapsed_seconds=excluded.elapsed_seconds,real_time_factor=excluded.real_time_factor,ram_delta_bytes=excluded.ram_delta_bytes,vram_delta_bytes=excluded.vram_delta_bytes,updated_at=excluded.updated_at""",
                (
                    project_id, job_id, request.model, result["language"], result["language_probability"], device, compute_type,
                    str(paths["json"]), str(paths["srt"]), str(paths["text"]), duration, len(segments), words, round(elapsed, 3),
                    round(elapsed / max(duration, 0.001), 4), ram_delta, vram_delta, now, now,
                ),
            )


def recover_interrupted_transcriptions() -> int:
    """Mark queued or running jobs interrupted by a server restart as retryable failures."""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET state='failed',stage='interrupted by restart',error='The local server restarted during transcription. Retry this job.',completed_at=? WHERE kind='transcription' AND state IN ('queued','running')",
            (now_iso(),),
        )
        return cursor.rowcount
