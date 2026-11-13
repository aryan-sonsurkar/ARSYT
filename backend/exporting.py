"""Local clip export to MP4, SRT, metadata JSON and batch ZIP files."""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Iterable

from backend.config import settings
from backend.database import get_setting, row
from backend.ingestion import IngestionError


def export_root() -> Path:
    configured = get_setting("export_dir", "").strip()
    return Path(configured).expanduser().resolve() if configured else settings.export_dir


def safe_name(value: str, fallback: str = "clip") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", value or "").strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned[:80] or fallback)


def _clip_files(clip: dict) -> list[Path]:
    files: list[Path] = []
    for key in ("output_path", "srt_path", "thumbnail_path"):
        path = Path(clip.get(key) or "")
        if path.is_file():
            files.append(path)
    return files


def export_clip(clip: dict, project_name: str) -> dict:
    if clip.get("state") != "rendered":
        raise IngestionError("Render this clip before exporting it.")
    output = Path(clip.get("output_path") or "")
    if not output.is_file():
        raise IngestionError("The rendered clip file is missing. Re-render this clip.")
    destination = export_root() / safe_name(project_name, "project")
    destination.mkdir(parents=True, exist_ok=True)
    base = safe_name(clip.get("title") or "clip")
    stem = f"{base}_{clip['id'][:6]}"
    exported: list[str] = []
    video_path = destination / f"{stem}.mp4"
    shutil.copy2(output, video_path)
    exported.append(str(video_path))

    srt_source = Path(clip.get("srt_path") or "")
    if srt_source.is_file():
        srt_path = destination / f"{stem}.srt"
        shutil.copy2(srt_source, srt_path)
        exported.append(str(srt_path))

    metadata = {}
    try:
        metadata = json.loads(clip.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    metadata_path = destination / f"{stem}.json"
    metadata_path.write_text(json.dumps({
        "title": clip.get("title", ""),
        "start_time": clip.get("start_time"),
        "end_time": clip.get("end_time"),
        "duration": clip.get("duration"),
        "source_project": project_name,
        "metadata": metadata,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    exported.append(str(metadata_path))

    thumbnail = Path(clip.get("thumbnail_path") or "")
    if thumbnail.is_file():
        thumb_path = destination / f"{stem}.jpg"
        shutil.copy2(thumbnail, thumb_path)
        exported.append(str(thumb_path))
    return {"clip_id": clip["id"], "directory": str(destination), "files": exported}


def build_zip(zip_path: Path, clip_ids: Iterable[str], project_name: str = "clipfree") -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = zip_path.with_suffix(".partial.zip")
    temporary.unlink(missing_ok=True)
    added = 0
    manifest: list[dict] = []
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for clip_id in clip_ids:
            clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
            if not clip or clip.get("state") != "rendered":
                continue
            output = Path(clip.get("output_path") or "")
            if not output.is_file():
                continue
            stem = f"{safe_name(clip.get('title') or 'clip')}_{clip['id'][:6]}"
            archive.write(output, f"{safe_name(project_name, 'project')}/{stem}.mp4")
            srt = Path(clip.get("srt_path") or "")
            if srt.is_file():
                archive.write(srt, f"{safe_name(project_name, 'project')}/{stem}.srt")
            metadata = Path(output).parent / "metadata.json"
            if metadata.is_file():
                archive.write(metadata, f"{safe_name(project_name, 'project')}/{stem}.metadata.json")
            manifest.append({
                "id": clip["id"], "title": clip.get("title"), "file": f"{stem}.mp4",
                "start_time": clip.get("start_time"), "end_time": clip.get("end_time"),
                "duration": clip.get("duration"),
            })
            added += 1
        archive.writestr("manifest.json", json.dumps({"clips": manifest}, ensure_ascii=False, indent=2))
    if not added:
        temporary.unlink(missing_ok=True)
        raise IngestionError("There are no rendered clips to export.")
    temporary.replace(zip_path)
    return zip_path


def list_exports() -> list[dict]:
    root = export_root()
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.rglob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in {".mp4", ".zip", ".srt", ".json", ".jpg"}:
            items.append({
                "name": path.name, "path": str(path), "relative_path": str(path.relative_to(root)),
                "size_bytes": path.stat().st_size, "modified_at": path.stat().st_mtime,
            })
    return items
