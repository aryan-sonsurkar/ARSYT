from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from database.models import Topic, Video
from database.repository import Repository


@pytest.fixture
def repo(tmp_path):
    """Create a Repository with a temporary database."""
    original_init = Repository.__init__

    def patched_init(self):
        self.settings = __import__("unittest.mock").MagicMock()
        self.settings.database_path = tmp_path / "test.db"
        self.db_path = str(self.settings.database_path)
        self._init_db = lambda: None
        # Minimal init
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER REFERENCES topics(id),
                title TEXT NOT NULL, script TEXT NOT NULL,
                description TEXT, hashtags TEXT, video_path TEXT,
                voice_path TEXT, duration_seconds REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                uploaded_youtube INTEGER DEFAULT 0,
                upload_attempts INTEGER DEFAULT 0,
                last_upload_error TEXT
            );
        """)
        conn.commit()
        conn.close()

    Repository.__init__ = patched_init
    r = Repository()
    yield r


class TestDatabase:
    def test_add_topic(self, repo):
        tid = repo.add_topic("Test Topic", "programming_concepts")
        assert tid > 0

    def test_duplicate_topic(self, repo):
        repo.add_topic("Unique Topic", "programming_languages")
        tid2 = repo.add_topic("Unique Topic", "programming_languages")
        assert tid2 > 0  # returns existing id

    def test_save_and_get_video(self, repo):
        v = Video(title="Test", script="Script content")
        vid = repo.save_video(v)
        assert vid > 0
        fetched = repo.get_video(vid)
        assert fetched is not None
        assert fetched.title == "Test"
        assert fetched.script == "Script content"

    def test_update_upload_status(self, repo):
        v = Video(title="Upload Test", script="Script")
        vid = repo.save_video(v)
        repo.update_upload_status(vid, "youtube", True)
        fetched = repo.get_video(vid)
        assert fetched.uploaded_youtube is True
        assert fetched.upload_attempts == 1
