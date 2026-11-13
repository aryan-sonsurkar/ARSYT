from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from backend.config import settings


class ClosingConnection(sqlite3.Connection):
    """Commit/roll back and release SQLite handles at the end of each context."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    settings.projects_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                source_filename TEXT NOT NULL DEFAULT '',
                source_size_bytes INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'queued',
                progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 100),
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transcripts (
                project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                model_name TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                language_probability REAL,
                device TEXT NOT NULL,
                compute_type TEXT NOT NULL,
                json_path TEXT NOT NULL,
                srt_path TEXT NOT NULL,
                text_path TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 0,
                segment_count INTEGER NOT NULL DEFAULT 0,
                word_count INTEGER NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                real_time_factor REAL,
                ram_delta_bytes INTEGER NOT NULL DEFAULT 0,
                vram_delta_bytes INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                hook TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                scores_json TEXT NOT NULL DEFAULT '{}',
                overall_score REAL NOT NULL DEFAULT 0,
                excerpt TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'candidate',
                selected INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'ollama',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                candidate_id TEXT REFERENCES candidates(id) ON DELETE SET NULL,
                title TEXT NOT NULL DEFAULT '',
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'draft',
                preset TEXT NOT NULL DEFAULT 'podcast',
                caption_style TEXT NOT NULL DEFAULT 'kinetic',
                caption_options_json TEXT NOT NULL DEFAULT '{}',
                music_path TEXT NOT NULL DEFAULT '',
                music_volume REAL NOT NULL DEFAULT 0.18,
                output_path TEXT NOT NULL DEFAULT '',
                thumbnail_path TEXT NOT NULL DEFAULT '',
                srt_path TEXT NOT NULL DEFAULT '',
                duration REAL NOT NULL DEFAULT 0,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id, overall_score DESC);
            CREATE INDEX IF NOT EXISTS idx_clips_project ON clips(project_id, created_at DESC);
        """)
        _add_column_if_missing(conn, "projects", "source_filename", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "projects", "source_size_bytes", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "projects", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(conn, "jobs", "kind", "TEXT NOT NULL DEFAULT 'ingestion'")
        _add_column_if_missing(conn, "jobs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "jobs", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "jobs", "payload_json", "TEXT NOT NULL DEFAULT '{}'")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {item["name"] for item in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    result = rows(query, params)
    return result[0] if result else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_setting(key: str, default: str = "") -> str:
    result = row("SELECT value FROM settings WHERE key=?", (key,))
    return result["value"] if result else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )


def all_settings() -> dict[str, str]:
    return {item["key"]: item["value"] for item in rows("SELECT key,value FROM settings")}
