from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from database.repository import Repository

logger = logging.getLogger(__name__)

CLEANUP_TARGETS: list[tuple[str, str]] = [
    ("images", "*.png"),
    ("images", "*.jpg"),
    ("voices", "*.wav"),
    ("voices", "*.mp3"),
    ("subtitles", "*.srt"),
    ("temp", "*.*"),
    ("cache", "*.*"),
    ("intermediate", "*.*"),
]

PROTECTED_DIRS = ["outputs", "database", "reports", "config", "models", "browser_profiles"]


class CleanupEngine:
    """Handles safe deletion of expired temporary assets."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()

    def run(self) -> dict:
        logger.info("Running cleanup engine...")
        deleted_count = 0
        space_recovered = 0.0
        folder_stats: dict[str, dict] = {}

        if not self._uploads_verified():
            logger.warning("Recent uploads not verified. Skipping cleanup.")
            return {"deleted": 0, "recovered_mb": 0, "error": "uploads not verified"}

        cutoff = datetime.now() - timedelta(days=self.settings.cleanup_retention_days)
        cutoff_ts = cutoff.timestamp()

        for label, pattern in CLEANUP_TARGETS:
            dir_map = {
                "images": self.settings.images_dir,
                "voices": self.settings.voices_dir,
                "subtitles": self.settings.subtitles_dir,
                "temp": self.settings.temp_dir,
                "cache": self.settings.cache_dir,
                "intermediate": self.settings.intermediate_dir,
            }
            directory = dir_map.get(label)
            if not directory or not directory.exists():
                continue

            folder_del = 0
            folder_space = 0.0
            for f in directory.glob(pattern):
                if not f.is_file():
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if mtime < cutoff_ts:
                        sz = f.stat().st_size / (1024 * 1024)
                        f.unlink()
                        folder_del += 1
                        folder_space += sz
                except OSError as e:
                    logger.warning("Cannot delete %s: %s", f, e)

            if folder_del > 0:
                deleted_count += folder_del
                space_recovered += folder_space
                folder_stats[label] = {
                    "deleted": folder_del,
                    "recovered_mb": round(folder_space, 2),
                    "current_size_mb": round(self._dir_size(directory), 2),
                }

        logger.info("Cleanup: %d files, %.2f MB recovered", deleted_count, space_recovered)
        return {
            "deleted": deleted_count,
            "recovered_mb": round(space_recovered, 2),
            "folder_stats": folder_stats,
        }

    def _dir_size(self, path: Path) -> float:
        total = 0.0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except OSError:
            pass
        return total / (1024 * 1024)

    def _uploads_verified(self) -> bool:
        videos = self.db.get_all_videos()
        recent = [
            v for v in videos
            if v.created_at
            and (isinstance(v.created_at, str)
                 and (datetime.now() - datetime.fromisoformat(v.created_at)).days < 7
                 or isinstance(v.created_at, datetime)
                 and (datetime.now() - v.created_at).days < 7)
        ]
        if not recent:
            return True
        return all(
            v.uploaded_youtube or v.status == "failed"
            for v in recent
        )

    def generate_report(self, result: dict) -> Path:
        now = datetime.now()
        report_path = self.settings.reports_dir / f"cleanup_report_{now.strftime('%Y-%m-%d')}.md"
        lines = [
            f"# Cleanup Report",
            f"**Date:** {now.strftime('%Y-%m-%d %H:%M')}",
            f"**Retention:** {self.settings.cleanup_retention_days} days",
            "",
            "## Summary",
            "",
            f"- **Files Deleted:** {result.get('deleted', 0)}",
            f"- **Space Recovered:** {result.get('recovered_mb', 0):.2f} MB",
            "",
            "## Folder Breakdown",
            "",
            "| Folder | Deleted | Recovered | Current Size |",
            "|--------|---------|-----------|--------------|",
        ]
        for label, stats in sorted(result.get("folder_stats", {}).items()):
            lines.append(
                f"| {label} | {stats['deleted']} | {stats['recovered_mb']:.2f} MB | {stats['current_size_mb']:.2f} MB |"
            )
        lines += [
            "",
            "## Disk Usage",
            "",
        ]
        try:
            usage = shutil.disk_usage(self.settings.project_root)
            lines.append(f"- **Usage:** {(usage.used / usage.total) * 100:.1f}%")
            lines.append(f"- **Free:** {usage.free / (1024**3):.1f} GB")
        except Exception:
            lines.append("- Could not determine disk usage")
        lines += ["", "---", "*CleanupEngine*", ""]
        content = "\n".join(lines) + "\n"
        report_path.write_text(content, encoding="utf-8")

        self.db.save_cleanup_report(
            str(report_path), result.get("deleted", 0), result.get("recovered_mb", 0), content
        )
        logger.info("Cleanup report: %s", report_path)
        return report_path
