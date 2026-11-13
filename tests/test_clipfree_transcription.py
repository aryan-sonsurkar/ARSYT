from __future__ import annotations

import json
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from backend.database import connect, init_db, now_iso, row
from backend.ingestion import IngestionError
from backend.jobs import IngestionJobManager
from backend.transcription import (
    TranscriptionRequest,
    TranscriptionService,
    WhisperModelManager,
    _process_memory_bytes,
    recover_interrupted_transcriptions,
    transcript_to_srt,
)


def test_srt_output_uses_actual_segment_timestamps() -> None:
    rendered = transcript_to_srt([
        {"start": 1.25, "end": 3.5, "text": "A timestamped sentence."},
        {"start": 4.0, "end": 5.125, "text": "Another sentence."},
    ])
    assert "00:00:01,250 --> 00:00:03,500" in rendered
    assert "A timestamped sentence." in rendered


def test_process_memory_probe_is_optional_and_non_negative() -> None:
    usage = _process_memory_bytes()
    assert usage is None or usage > 0


def test_database_connection_closes_after_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    with connect() as connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_model_inventory_does_not_download_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    inventory = WhisperModelManager().inventory()
    assert {item["name"] for item in inventory} == {"tiny", "base", "small", "medium"}
    assert not any(item["installed"] for item in inventory)
    assert not (tmp_path / "data" / "models").exists()


def test_transcription_rejects_missing_model_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    project_id = "transcriptiontest"
    project_root = tmp_path / "data" / "projects" / project_id
    (project_root / "audio").mkdir(parents=True)
    (project_root / "audio" / "source_audio.wav").write_bytes(b"not used before model validation")
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)",
            (project_id, "Test", now_iso(), now_iso()),
        )
    service = TranscriptionService(IngestionJobManager())
    monkeypatch.setattr(service.models, "engine_available", lambda: True)
    with pytest.raises(IngestionError, match="not installed locally"):
        import asyncio
        asyncio.run(service.start(project_id, TranscriptionRequest("tiny", None, True, "auto")))


def test_restart_recovery_marks_running_transcription_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    project_id, job_id, queued_job_id = "restartproject", "restartjob", "queuedjob"
    with connect() as conn:
        conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)", (project_id, "Restart", now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, project_id, "transcription", "running", "transcribing audio", 40, now_iso()),
        )
        conn.execute(
            "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
            (queued_job_id, project_id, "transcription", "queued", "queued", 0, now_iso()),
        )
    assert recover_interrupted_transcriptions() == 2
    recovered = row("SELECT state,stage,error FROM jobs WHERE id=?", (job_id,))
    assert recovered == {
        "state": "failed",
        "stage": "interrupted by restart",
        "error": "The local server restarted during transcription. Retry this job.",
    }
    assert row("SELECT state FROM jobs WHERE id=?", (queued_job_id,)) == {"state": "failed"}


def test_transcription_start_requires_installed_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    service = TranscriptionService(IngestionJobManager())
    monkeypatch.setattr(service.models, "engine_available", lambda: False)

    import asyncio
    with pytest.raises(IngestionError, match="faster-whisper dependency is not installed"):
        asyncio.run(service.start("missing-project", TranscriptionRequest("base", None, True, "auto")))


def test_transcription_start_rejects_duplicate_active_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    project_id = "active-transcription"
    project_root = tmp_path / "data" / "projects" / project_id
    (project_root / "audio").mkdir(parents=True)
    (project_root / "audio" / "source_audio.wav").write_bytes(b"audio")
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,source_path,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_id, "Active", str(project_root / "source" / "source.mp4"), now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
            ("active-job", project_id, "transcription", "running", "transcribing audio", 20, now_iso()),
        )
    service = TranscriptionService(IngestionJobManager())
    monkeypatch.setattr(service.models, "engine_available", lambda: True)
    monkeypatch.setattr(service.models, "find_local_model", lambda _: tmp_path)

    import asyncio
    with pytest.raises(IngestionError, match="already active"):
        asyncio.run(service.start(project_id, TranscriptionRequest("base", None, True, "auto")))


def test_auto_device_falls_back_to_cpu_when_cuda_runtime_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    devices: list[str] = []

    class Model:
        def __init__(self, _model_path: str, *, device: str, **_kwargs: object) -> None:
            devices.append(device)
            self.device = device

        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], object]:
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            return [], SimpleNamespace(language="en", language_probability=0.9)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    project_id, job_id = "fallback-project", "fallback-job"
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)",
            (project_id, "Fallback", now_iso(), now_iso()),
        )
    manager = IngestionJobManager()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, project_id, "transcription", "running", "loading", 0, now_iso()),
        )
    service = TranscriptionService(manager)

    result, device, compute_type = service._transcribe_blocking(
        tmp_path, tmp_path / "audio.wav", TranscriptionRequest("small", None, True, "auto"),
        "cuda", "int8_float16", 10, job_id,
    )
    assert devices == ["cuda", "cpu"]
    assert (device, compute_type) == ("cpu", "int8")
    assert result["language"] == "en"
