"""Shared FFmpeg process execution with real progress and cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from backend.ingestion import IngestionError, JobCancelled

Progress = Callable[[float], Awaitable[None]]


async def _read_tail(stream: asyncio.StreamReader | None, limit: int = 12_000) -> str:
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


async def run_ffmpeg(
    arguments: list[str],
    duration_seconds: float,
    cancel_event: asyncio.Event,
    progress: Progress,
    *,
    log_path: Path | None = None,
    cwd: Path | None = None,
) -> None:
    """Run FFmpeg with ``-progress pipe:1``, bounded memory and true telemetry.

    ``arguments`` must not include the executable, ``-progress`` or output path
    hidden inside a shell string: each argument is passed directly to FFmpeg.
    """
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *arguments, "-progress", "pipe:1", "-nostats"]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise IngestionError("FFmpeg is unavailable. Install FFmpeg and add it to PATH.") from exc

    stderr_task = asyncio.create_task(_read_tail(process.stderr))
    last_reported = -1.0
    cancelled = False
    try:
        assert process.stdout is not None
        while True:
            if cancel_event.is_set():
                cancelled = True
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                raise JobCancelled("Render cancelled by user.")
            line = await process.stdout.readline()
            if not line:
                break
            key, _, value = line.decode("utf-8", errors="replace").strip().partition("=")
            if key in {"out_time_us", "out_time_ms"} and duration_seconds > 0:
                seconds = float(value or 0) / 1_000_000
                percent = max(0.0, min(99.0, seconds / duration_seconds * 100.0))
                if percent - last_reported >= 0.5:
                    last_reported = percent
                    await progress(round(percent, 2))
        return_code = await process.wait()
        stderr = await stderr_task
        if return_code or cancelled:
            details = stderr[-1400:].strip() or "FFmpeg exited without details."
            raise IngestionError(f"FFmpeg failed: {details}")
    except JobCancelled:
        raise
    except Exception:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        if log_path is not None:
            try:
                if stderr_task.done() and not stderr_task.cancelled():
                    log_path.write_text(stderr_task.result(), encoding="utf-8")
            except Exception:
                pass
