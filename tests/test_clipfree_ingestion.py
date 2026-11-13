from __future__ import annotations

import asyncio
from starlette.datastructures import Headers, UploadFile
import subprocess
from pathlib import Path

import pytest

from backend.ingestion import (
    IngestionError,
    JobCancelled,
    _validate_container_signature,
    extract_wav_audio,
    probe_media,
    sanitize_filename,
    stream_upload,
)


@pytest.fixture()
def sample_video(tmp_path: Path) -> Path:
    """Produce a small genuine video+audio container for local ingestion tests."""
    output = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(output),
        ],
        check=True,
    )
    return output


def test_upload_filename_validation_removes_paths_and_rejects_other_types() -> None:
    assert sanitize_filename(r"C:\untrusted\..\episode!.mp4") == "episode_.mp4"
    with pytest.raises(IngestionError, match="Unsupported file type"):
        sanitize_filename("not-a-video.exe")


def test_ffprobe_returns_actual_video_and_audio_metadata(sample_video: Path) -> None:
    metadata = probe_media(sample_video)
    assert metadata["video"]["width"] == 320
    assert metadata["video"]["height"] == 240
    assert metadata["audio"]["available"] is True
    assert 0.9 <= metadata["duration_seconds"] <= 1.1


def test_streamed_upload_writes_video_in_chunks(sample_video: Path, tmp_path: Path) -> None:
    destination = tmp_path / "streamed.mp4"
    source_size = sample_video.stat().st_size
    received_progress: list[float] = []

    async def record(value: float) -> None:
        received_progress.append(value)

    with sample_video.open("rb") as source:
        upload = UploadFile(
            file=source,
            filename="..\\authorized sample.mp4",
            headers=Headers({"content-length": str(source_size)}),
        )
        bytes_written = asyncio.run(stream_upload(upload, destination, asyncio.Event(), record))
    assert bytes_written == source_size
    assert destination.stat().st_size == source_size
    assert received_progress and received_progress[-1] == 55.0


def test_audio_extraction_produces_16khz_mono_wav(sample_video: Path, tmp_path: Path) -> None:
    output = tmp_path / "audio.wav"
    progress: list[float] = []

    async def record(value: float) -> None:
        progress.append(value)

    info = asyncio.run(
        extract_wav_audio(sample_video, output, 1.0, asyncio.Event(), record)
    )
    assert output.is_file()
    assert info["codec"] == "pcm_s16le"
    assert info["sample_rate"] == 16000
    assert info["channels"] == 1
    assert info["duration_seconds"] > 0


def test_invalid_video_bytes_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "fake.mp4"
    invalid.write_bytes(b"this is not a media container")
    with pytest.raises(IngestionError, match="recognized video container"):
        _validate_container_signature(invalid)


def test_interrupted_audio_job_cleans_partial_output(sample_video: Path, tmp_path: Path) -> None:
    output = tmp_path / "audio.wav"
    cancel = asyncio.Event()
    cancel.set()

    async def no_op(_: float) -> None:
        return None

    with pytest.raises(JobCancelled, match="cancelled"):
        asyncio.run(extract_wav_audio(sample_video, output, 1.0, cancel, no_op))
    assert not output.exists()
    assert not (tmp_path / "audio.partial.wav").exists()
