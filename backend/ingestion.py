"""Streaming local-media ingestion primitives for ClipFree.

These functions only move bytes in bounded chunks and invoke FFmpeg/FFprobe as
subprocesses. They deliberately do not load source videos into memory.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from fastapi import UploadFile

from backend.config import settings

ALLOWED_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
ProgressCallback = Callable[[float], Awaitable[None]]


class IngestionError(RuntimeError):
    """A source could not be safely ingested or processed."""


class JobCancelled(RuntimeError):
    """The caller cancelled the current operation."""


def sanitize_filename(filename: str | None) -> str:
    """Return a safe basename with a supported extension, never a client path."""
    raw_name = Path(filename or "video.mp4").name
    clean_name = re.sub(r"[^A-Za-z0-9._ -]", "_", raw_name).strip(" .")
    if not clean_name:
        raise IngestionError("The uploaded file needs a valid filename.")
    if Path(clean_name).suffix.lower() not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise IngestionError(f"Unsupported file type. Use one of: {allowed}.")
    return clean_name[:180]


def _assert_safe_path(path: Path, root: Path) -> None:
    if root.resolve() not in path.resolve().parents:
        raise IngestionError("The requested media path is outside the project source folder.")


def _validate_container_signature(path: Path) -> None:
    with path.open("rb") as media_file:
        header = media_file.read(16)
    is_iso_media = len(header) >= 8 and header[4:8] == b"ftyp"
    is_matroska = header.startswith(b"\x1aE\xdf\xa3")
    is_avi = header.startswith(b"RIFF") and header[8:12] == b"AVI "
    if not (is_iso_media or is_matroska or is_avi):
        raise IngestionError("The file does not have a recognized video container signature.")


def _parse_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return round(float(numerator) / float(denominator), 3) if float(denominator) else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: Path) -> dict:
    """Read compact FFprobe JSON. FFprobe streams from disk; media stays out of RAM."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=format_name,duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IngestionError("FFprobe is unavailable. Install FFmpeg and add it to PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise IngestionError("FFprobe timed out while reading this file.") from exc
    if result.returncode:
        message = result.stderr.strip()[-800:] or "FFprobe could not read this media file."
        raise IngestionError(f"Invalid or corrupt media: {message}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IngestionError("FFprobe returned unreadable metadata.") from exc

    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        raise IngestionError("The file has no video stream. Upload a video with a video track.")
    container = data.get("format", {})
    duration = float(container.get("duration", 0) or 0)
    if duration <= 0:
        raise IngestionError("The file has no usable duration.")
    return {
        "duration_seconds": round(duration, 3),
        "file_size_bytes": int(float(container.get("size", path.stat().st_size) or path.stat().st_size)),
        "format": container.get("format_name", ""),
        "video": {
            "codec": video.get("codec_name", ""),
            "width": video.get("width"),
            "height": video.get("height"),
            "frame_rate": _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        },
        "audio": {
            "available": audio is not None,
            "codec": audio.get("codec_name", "") if audio else "",
            "sample_rate": int(audio.get("sample_rate", 0) or 0) if audio else 0,
            "channels": int(audio.get("channels", 0) or 0) if audio else 0,
        },
    }


async def stream_upload(
    upload: UploadFile,
    destination: Path,
    cancel_event: asyncio.Event,
    progress: ProgressCallback,
) -> int:
    """Persist a multipart upload in 1 MiB chunks and report real byte progress."""
    content_length = upload.headers.get("content-length")
    expected_bytes = int(content_length) if content_length and content_length.isdigit() else 0
    if expected_bytes > settings.max_upload_bytes:
        raise IngestionError("The upload exceeds ClipFree's configured source size limit.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    try:
        with destination.open("xb") as output:
            while True:
                if cancel_event.is_set():
                    raise JobCancelled("Upload cancelled by user.")
                chunk = await upload.read(settings.upload_chunk_bytes)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > settings.max_upload_bytes:
                    raise IngestionError("The upload exceeds ClipFree's configured source size limit.")
                output.write(chunk)
                if expected_bytes:
                    await progress(min(55.0, 2.0 + (bytes_written / expected_bytes) * 53.0))
        if not bytes_written:
            raise IngestionError("The uploaded file is empty.")
        return bytes_written
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def validate_authorized_direct_url(url: str) -> None:
    """Permit public direct HTTPS/HTTP media only; reject restricted platform URLs."""
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
        raise IngestionError("Use a public http:// or https:// direct media URL without credentials.")
    restricted_hosts = ("youtube.com", "youtu.be", "vimeo.com", "spotify.com", "tiktok.com", "instagram.com", "facebook.com")
    lower_host = host.lower()
    if any(lower_host == item or lower_host.endswith(f".{item}") for item in restricted_hosts):
        raise IngestionError(
            "This platform URL is not a direct media file. ClipFree will not bypass platform download, login, paywall, or DRM restrictions. Download an authorized copy and upload it locally."
        )
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise IngestionError("The source host could not be resolved.") from exc
    for address in addresses:
        parsed_ip = ipaddress.ip_address(address)
        if not parsed_ip.is_global:
            raise IngestionError("Source URLs must resolve to a public internet address.")


async def stream_direct_url(
    url: str,
    destination: Path,
    cancel_event: asyncio.Event,
    progress: ProgressCallback,
) -> tuple[int, str]:
    """Download an authorized direct file in bounded chunks with redirect revalidation."""
    current_url = url
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(20.0, read=60.0)) as client:
            for _ in range(5):
                validate_authorized_direct_url(current_url)
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        next_url = response.headers.get("location")
                        if not next_url:
                            raise IngestionError("The source redirected without a destination.")
                        current_url = str(httpx.URL(current_url).join(next_url))
                        continue
                    if response.status_code >= 400:
                        raise IngestionError(f"The source returned HTTP {response.status_code}.")
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and "video" not in content_type and "octet-stream" not in content_type:
                        raise IngestionError("The URL did not return a video file.")
                    content_length = int(response.headers.get("content-length", "0") or 0)
                    if content_length > settings.max_upload_bytes:
                        raise IngestionError("The remote source exceeds ClipFree's configured source size limit.")
                    written = 0
                    with destination.open("xb") as output:
                        async for chunk in response.aiter_bytes(settings.upload_chunk_bytes):
                            if cancel_event.is_set():
                                raise JobCancelled("URL import cancelled by user.")
                            written += len(chunk)
                            if written > settings.max_upload_bytes:
                                raise IngestionError("The remote source exceeds ClipFree's configured source size limit.")
                            output.write(chunk)
                            if content_length:
                                await progress(min(55.0, 2.0 + (written / content_length) * 53.0))
                    if not written:
                        raise IngestionError("The URL returned an empty file.")
                    filename = sanitize_filename(Path(urlparse(current_url).path).name or "download.mp4")
                    return written, filename
        raise IngestionError("The URL redirected too many times.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


async def extract_wav_audio(
    source: Path,
    destination: Path,
    duration_seconds: float,
    cancel_event: asyncio.Event,
    progress: ProgressCallback,
) -> dict:
    """Run FFmpeg with progress telemetry and atomically publish a 16 kHz mono WAV."""
    temporary = destination.with_name(f"{destination.stem}.partial.wav")
    temporary.unlink(missing_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(source), "-map", "0:a:0",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-progress", "pipe:1",
        "-nostats", str(temporary),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise IngestionError("FFmpeg is unavailable. Install FFmpeg and add it to PATH.") from exc

    stderr_task = asyncio.create_task(_read_limited(process.stderr))
    try:
        assert process.stdout is not None
        while True:
            if cancel_event.is_set():
                process.terminate()
                await process.wait()
                raise JobCancelled("Audio extraction cancelled by user.")
            line = await process.stdout.readline()
            if not line:
                break
            key, _, value = line.decode("utf-8", errors="replace").strip().partition("=")
            if key == "out_time_ms" and duration_seconds:
                seconds = float(value or 0) / 1_000_000
                await progress(min(98.0, 65.0 + max(0.0, min(1.0, seconds / duration_seconds)) * 33.0))
        return_code = await process.wait()
        stderr = await stderr_task
        if return_code:
            details = stderr[-900:] or "FFmpeg could not extract an audio stream."
            raise IngestionError(f"Audio extraction failed: {details}")
        temporary.replace(destination)
        audio_probe = await asyncio.to_thread(probe_audio, destination)
        return audio_probe
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def _read_limited(stream: asyncio.StreamReader | None, limit: int = 16_000) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    stored = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
        stored += len(chunk)
        while stored > limit and chunks:
            stored -= len(chunks.pop(0))
    return b"".join(chunks).decode("utf-8", errors="replace")


def probe_audio_file(path: Path) -> dict:
    """Validate any local audio file used for optional background music."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,sample_rate,channels:format=duration,size",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise IngestionError("Could not verify the audio file.") from exc
    stream = next((item for item in data.get("streams", []) if item.get("codec_type") == "audio"), None)
    if result.returncode or not stream:
        raise IngestionError("The selected file does not contain an audio stream.")
    info = data.get("format", {})
    return {
        "codec": stream.get("codec_name", ""),
        "sample_rate": int(stream.get("sample_rate", 0) or 0),
        "channels": int(stream.get("channels", 0) or 0),
        "duration_seconds": round(float(info.get("duration", 0) or 0), 3),
        "file_size_bytes": int(float(info.get("size", path.stat().st_size) or path.stat().st_size)),
    }


def probe_audio(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels:format=duration,size", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise IngestionError("Could not verify the extracted WAV file.") from exc
    stream = next((item for item in data.get("streams", []) if item.get("codec_name")), None)
    if result.returncode or not stream or int(stream.get("sample_rate", 0) or 0) != 16000 or int(stream.get("channels", 0) or 0) != 1:
        raise IngestionError("FFmpeg did not produce a valid 16 kHz mono WAV file.")
    info = data.get("format", {})
    return {
        "path": str(path),
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "duration_seconds": round(float(info.get("duration", 0) or 0), 3),
        "file_size_bytes": int(float(info.get("size", path.stat().st_size) or path.stat().st_size)),
    }
