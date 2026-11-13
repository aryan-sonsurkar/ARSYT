from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from database.repository import Repository

logger = logging.getLogger(__name__)


class StorageMonitor:
    """Disk usage monitoring and asset cleanup system."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()

    def run_cleanup(self) -> Path:
        """Full cleanup pipeline: monitor, delete, report."""
        logger.info("=" * 60)
        logger.info("Starting storage cleanup")
        logger.info("=" * 60)

        self._check_disk_usage()

        deleted_count = 0
        space_recovered = 0.0
        folder_breakdown: dict[str, dict] = {}

        cleanup_dirs = [
            ("images", self.settings.images_dir, "*.png"),
            ("images", self.settings.images_dir, "*.jpg"),
            ("voices", self.settings.voices_dir, "*.wav"),
            ("voices", self.settings.voices_dir, "*.mp3"),
            ("subtitles", self.settings.subtitles_dir, "*.srt"),
            ("temp", self.settings.temp_dir, "*.*"),
            ("cache", self.settings.cache_dir, "*.*"),
            ("intermediate", self.settings.intermediate_dir, "*.*"),
        ]

        for label, directory, pattern in cleanup_dirs:
            if not directory.exists():
                continue
            folder_result = self._cleanup_directory(
                directory, pattern, label
            )
            if folder_result:
                d, s = folder_result
                deleted_count += d
                space_recovered += s
                folder_breakdown[label] = {
                    "files_deleted": d,
                    "space_recovered_mb": round(s, 2),
                    "current_size_mb": round(
                        self._get_dir_size_mb(directory), 2
                    ),
                }

        self._check_temp_size()

        report_path = self._generate_cleanup_report(
            deleted_count, space_recovered, folder_breakdown
        )

        if deleted_count > 0 and self._all_uploads_verified():
            logger.info(
                "Cleanup complete: %d files deleted, %.2f MB recovered",
                deleted_count,
                space_recovered,
            )
        elif deleted_count > 0:
            logger.warning(
                "Cleanup performed but some uploads could not be verified"
            )

        return report_path

    def _cleanup_directory(
        self, directory: Path, pattern: str, label: str
    ) -> Optional[tuple[int, float]]:
        if not directory.exists():
            return None

        cutoff = datetime.now() - timedelta(
            days=self.settings.cleanup_retention_days
        )
        cutoff_ts = cutoff.timestamp()

        deleted = 0
        space = 0.0

        for f in directory.glob(pattern):
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
                if mtime < cutoff_ts:
                    file_size = f.stat().st_size / (1024 * 1024)
                    f.unlink()
                    deleted += 1
                    space += file_size
            except OSError as e:
                logger.warning(
                    "Could not delete %s: %s", f, e
                )

        if deleted > 0:
            logger.info(
                "Cleaned %s: %d files, %.2f MB recovered",
                label,
                deleted,
                space,
            )
        return (deleted, space) if deleted > 0 else None

    def _check_disk_usage(self) -> None:
        try:
            usage = shutil.disk_usage(self.settings.project_root)
            percent_used = (usage.used / usage.total) * 100
            free_gb = usage.free / (1024 ** 3)

            if percent_used > self.settings.storage_warning_threshold:
                logger.warning(
                    "Disk usage at %.1f%% (threshold: %d%%)",
                    percent_used,
                    self.settings.storage_warning_threshold,
                )
            if free_gb < self.settings.free_storage_warning_gb:
                logger.warning(
                    "Free space: %.1f GB (threshold: %d GB)",
                    free_gb,
                    self.settings.free_storage_warning_gb,
                )

            logger.info(
                "Disk: %.1f%% used, %.1f GB free",
                percent_used,
                free_gb,
            )
        except Exception as e:
            logger.warning("Could not check disk usage: %s", e)

    def _check_temp_size(self) -> None:
        temp_size_mb = self._get_dir_size_mb(self.settings.temp_dir)
        if temp_size_mb > self.settings.max_temp_size_mb:
            logger.warning(
                "Temp directory size: %.1f MB (limit: %d MB)",
                temp_size_mb,
                self.settings.max_temp_size_mb,
            )

    def _get_dir_size_mb(self, directory: Path) -> float:
        if not directory.exists():
            return 0.0
        total = 0.0
        try:
            for f in directory.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except OSError:
            pass
        return total / (1024 * 1024)

    def _all_uploads_verified(self) -> bool:
        videos = self.db.get_all_videos()
        recent = [
            v
            for v in videos
            if v.created_at
            and (
                isinstance(v.created_at, str)
                and (
                    datetime.now()
                    - datetime.fromisoformat(v.created_at)
                ).days
                < 7
            )
            or (
                isinstance(v.created_at, datetime)
                and (datetime.now() - v.created_at).days < 7
            )
        ]
        if not recent:
            return True
        return all(
            v.uploaded_youtube or v.status == "failed"
            for v in recent
        )

    def _generate_cleanup_report(
        self,
        deleted_count: int,
        space_recovered: float,
        folder_breakdown: dict,
    ) -> Path:
        now = datetime.now()
        report_dir = self.settings.reports_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = (
            report_dir / f"{now.strftime('%Y-%m-%d')}-cleanup-report.md"
        )

        lines = [
            f"# Storage Cleanup Report",
            f"**Date:** {now.strftime('%Y-%m-%d')}",
            "",
            "## Summary",
            "",
            f"- **Files Deleted:** {deleted_count}",
            f"- **Space Recovered:** {space_recovered:.2f} MB",
            "",
            "## Folder Breakdown",
            "",
            "| Folder | Files Deleted | Space Recovered | Current Size |",
            "|--------|---------------|-----------------|--------------|",
        ]

        for label, info in sorted(folder_breakdown.items()):
            lines.append(
                f"| {label} | {info['files_deleted']} | "
                f"{info['space_recovered_mb']:.2f} MB | "
                f"{info['current_size_mb']:.2f} MB |"
            )

        lines += [
            "",
            "## Disk Usage",
            "",
        ]
        try:
            usage = shutil.disk_usage(self.settings.project_root)
            percent = (usage.used / usage.total) * 100
            free_gb = usage.free / (1024 ** 3)
            lines.append(f"- **Usage:** {percent:.1f}%")
            lines.append(f"- **Free:** {free_gb:.1f} GB")
            lines.append(f"- **Total:** {usage.total / (1024**3):.1f} GB")
        except Exception as e:
            lines.append(f"- *Could not determine disk usage: {e}*")

        lines += [
            "",
            "---",
            "*Report generated automatically by CodeShortsBot Storage Monitor*",
            "",
        ]

        content = "\n".join(lines) + "\n"
        report_path.write_text(content, encoding="utf-8")

        self.db.save_cleanup_report(
            str(report_path), deleted_count, space_recovered, content
        )

        logger.info("Cleanup report written: %s", report_path)
        return report_path
