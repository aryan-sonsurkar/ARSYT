from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.system.cleanup import CleanupEngine


class TestCleanupEngine:
    @patch("src.system.cleanup.CleanupEngine._uploads_verified")
    def test_cleanup_empty_dir(self, mock_verified, tmp_path):
        mock_verified.return_value = True
        engine = CleanupEngine()
        engine.settings.images_dir = tmp_path / "images"
        engine.settings.voices_dir = tmp_path / "voices"
        engine.settings.subtitles_dir = tmp_path / "subtitles"
        engine.settings.temp_dir = tmp_path / "temp"
        engine.settings.cache_dir = tmp_path / "cache"
        engine.settings.intermediate_dir = tmp_path / "intermediate"
        for d in [engine.settings.images_dir, engine.settings.voices_dir,
                  engine.settings.subtitles_dir, engine.settings.temp_dir,
                  engine.settings.cache_dir, engine.settings.intermediate_dir]:
            d.mkdir(parents=True, exist_ok=True)

        result = engine.run()
        assert result["deleted"] == 0
        assert result["recovered_mb"] == 0.0

    def test_dir_size(self, tmp_path):
        engine = CleanupEngine()
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        size = engine._dir_size(tmp_path)
        assert size > 0.0

    @patch("src.system.cleanup.datetime")
    def test_run_with_expired_files(self, mock_dt, tmp_path):
        import time
        from datetime import datetime, timedelta

        mock_dt.now.return_value = datetime(2026, 6, 14)

        engine = CleanupEngine()
        engine.settings.images_dir = tmp_path / "images"
        engine.settings.cleanup_retention_days = 7
        engine.settings.images_dir.mkdir(parents=True, exist_ok=True)

        old_file = engine.settings.images_dir / "old.png"
        old_file.write_text("old data")
        old_ts = time.mktime(datetime(2026, 6, 1).timetuple())
        import os
        os.utime(str(old_file), (old_ts, old_ts))

        engine.db = MagicMock()
        engine.db.get_all_videos.return_value = []

        result = engine.run()
        assert not old_file.exists() or result["deleted"] > 0
