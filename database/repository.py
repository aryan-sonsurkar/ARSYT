from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings
from database.models import Topic, Video

logger = logging.getLogger(__name__)


class Repository:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.db_path = str(self.settings.database_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        try:
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
                    title TEXT NOT NULL,
                    script TEXT NOT NULL,
                    description TEXT,
                    hashtags TEXT,
                    video_path TEXT,
                    voice_path TEXT,
                    duration_seconds REAL DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    uploaded_youtube INTEGER DEFAULT 0,
                    upload_attempts INTEGER DEFAULT 0,
                    last_upload_error TEXT
                );

                CREATE TABLE IF NOT EXISTS analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL REFERENCES videos(id),
                    platform TEXT NOT NULL,
                    upload_date TIMESTAMP,
                    title TEXT,
                    topic TEXT,
                    category TEXT,
                    script TEXT,
                    duration_seconds REAL DEFAULT 0,
                    views INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    comments INTEGER DEFAULT 0,
                    shares INTEGER DEFAULT 0,
                    engagement_rate REAL DEFAULT 0.0,
                    retention_data TEXT,
                    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS category_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT UNIQUE NOT NULL,
                    weight REAL DEFAULT 1.0,
                    rolling_30day_avg REAL,
                    total_views INTEGER DEFAULT 0,
                    total_likes INTEGER DEFAULT 0,
                    total_uploads INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS strategy_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    report_path TEXT,
                    content TEXT,
                    recommendations TEXT
                );

                CREATE TABLE IF NOT EXISTS cleanup_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cleanup_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    report_path TEXT,
                    files_deleted INTEGER DEFAULT 0,
                    space_recovered_mb REAL DEFAULT 0.0,
                    content TEXT
                );

                CREATE TABLE IF NOT EXISTS source_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    source_name TEXT,
                    title TEXT,
                    topic TEXT,
                    category TEXT,
                    url TEXT,
                    score REAL DEFAULT 0,
                    rank_score REAL DEFAULT 0,
                    fetched_at TIMESTAMP,
                    used INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS topic_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    category TEXT,
                    hook TEXT,
                    angle TEXT,
                    score_curiosity REAL DEFAULT 0,
                    score_novelty REAL DEFAULT 0,
                    score_educational REAL DEFAULT 0,
                    score_viral REAL DEFAULT 0,
                    overall REAL DEFAULT 0,
                    selected INTEGER DEFAULT 0,
                    source_inspiration TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS quality_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT,
                    category TEXT,
                    title TEXT,
                    script_length INTEGER DEFAULT 0,
                    score_hook REAL DEFAULT 0,
                    score_educational REAL DEFAULT 0,
                    score_originality REAL DEFAULT 0,
                    score_engagement REAL DEFAULT 0,
                    score_clarity REAL DEFAULT 0,
                    overall REAL DEFAULT 0,
                    passed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_topics_topic ON topics(topic);
                CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
                CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created_at);
                CREATE INDEX IF NOT EXISTS idx_analytics_video ON analytics(video_id);
                CREATE INDEX IF NOT EXISTS idx_analytics_platform ON analytics(platform);
                CREATE INDEX IF NOT EXISTS idx_analytics_category ON analytics(category);
                CREATE INDEX IF NOT EXISTS idx_category_weights_cat ON category_weights(category);
            """)
            conn.commit()
            self._seed_category_weights(conn)
            logger.info("Database initialized at %s", self.db_path)
        except sqlite3.Error as e:
            logger.error("Database init failed: %s", e)
            raise
        finally:
            conn.close()

    def _seed_category_weights(self, conn: sqlite3.Connection) -> None:
        categories = [
            "programming_concepts",
            "software_engineering_stories",
            "programming_languages",
            "open_source_projects",
            "developer_productivity",
            "computer_science_facts",
        ]
        for cat in categories:
            conn.execute(
                "INSERT OR IGNORE INTO category_weights (category, weight) VALUES (?, 1.0)",
                (cat,),
            )
        conn.commit()

    # ---- Topic methods ----

    def add_topic(self, topic: str, category: str) -> int:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO topics (topic, category) VALUES (?, ?)",
                (topic, category),
            )
            conn.commit()
            if cur.lastrowid:
                logger.info("Added topic: %s [%s]", topic, category)
                return cur.lastrowid
            existing = conn.execute(
                "SELECT id FROM topics WHERE topic = ?", (topic,)
            ).fetchone()
            return existing["id"] if existing else 0
        except sqlite3.Error as e:
            logger.error("Failed to add topic: %s", e)
            raise
        finally:
            conn.close()

    def get_category_for_topic_id(self, topic_id: int) -> str:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT category FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()
            return row["category"] if row else ""
        finally:
            conn.close()

    def get_unused_topics(self) -> list[Topic]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM topics ORDER BY used_at DESC"
            ).fetchall()
            used_ids = {
                r["topic_id"]
                for r in conn.execute("SELECT topic_id FROM videos").fetchall()
                if r["topic_id"]
            }
            result = []
            for row in rows:
                if row["id"] not in used_ids:
                    result.append(
                        Topic(
                            id=row["id"],
                            topic=row["topic"],
                            category=row["category"],
                            used_at=datetime.fromisoformat(row["used_at"])
                            if isinstance(row["used_at"], str)
                            else row["used_at"],
                        )
                    )
            return result
        finally:
            conn.close()

    def is_topic_used(self, topic: str) -> bool:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM topics WHERE topic = ?", (topic,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ---- Video methods ----

    def save_video(self, video: Video) -> int:
        conn = self._get_connection()
        try:
            if video.id:
                conn.execute(
                    """UPDATE videos SET title=?, script=?, description=?,
                       hashtags=?, video_path=?, voice_path=?, duration_seconds=?,
                       status=?, uploaded_youtube=?,
                       upload_attempts=?, last_upload_error=?
                       WHERE id=?""",
                    (
                        video.title, video.script, video.description,
                        video.hashtags, video.video_path, video.voice_path,
                        video.duration_seconds, video.status,
                        int(video.uploaded_youtube),
                        video.upload_attempts, video.last_upload_error,
                        video.id,
                    ),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO videos (topic_id, title, script, description,
                       hashtags, video_path, voice_path, duration_seconds, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        video.topic_id, video.title, video.script,
                        video.description, video.hashtags, video.video_path,
                        video.voice_path, video.duration_seconds, video.status,
                    ),
                )
                video.id = cur.lastrowid
            conn.commit()
            return video.id or 0
        except sqlite3.Error as e:
            logger.error("Failed to save video: %s", e)
            raise
        finally:
            conn.close()

    def get_video(self, video_id: int) -> Optional[Video]:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if row:
                return self._row_to_video(row)
            return None
        finally:
            conn.close()

    def get_latest_video(self) -> Optional[Video]:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM videos ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return self._row_to_video(row) if row else None
        finally:
            conn.close()

    def get_all_videos(self) -> list[Video]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM videos ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_video(r) for r in rows]
        finally:
            conn.close()

    def get_videos_in_range(self, since: datetime, until: datetime) -> list[Video]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM videos WHERE created_at >= ? AND created_at <= ? ORDER BY created_at",
                (since.isoformat(), until.isoformat()),
            ).fetchall()
            return [self._row_to_video(r) for r in rows]
        finally:
            conn.close()

    def _row_to_video(self, row: sqlite3.Row) -> Video:
        return Video(
            id=row["id"],
            topic_id=row["topic_id"],
            title=row["title"],
            script=row["script"],
            description=row["description"],
            hashtags=row["hashtags"],
            video_path=row["video_path"],
            voice_path=row["voice_path"],
            duration_seconds=row["duration_seconds"],
            status=row["status"],
            created_at=row["created_at"],
            uploaded_youtube=bool(row["uploaded_youtube"]),
            upload_attempts=row["upload_attempts"],
            last_upload_error=row["last_upload_error"],
        )

    def update_upload_status(
        self, video_id: int, platform: str, success: bool, error: str = ""
    ) -> None:
        conn = self._get_connection()
        try:
            set_parts: list[str] = []
            values: list = []
            if platform == "youtube":
                set_parts.append("uploaded_youtube = ?")
                values.append(1 if success else 0)
            set_parts.append("upload_attempts = upload_attempts + 1")
            if error:
                set_parts.append("last_upload_error = ?")
                values.append(error)
            values.append(video_id)
            conn.execute(
                f"UPDATE videos SET {', '.join(set_parts)} WHERE id = ?",
                values,
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to update upload status: %s", e)
        finally:
            conn.close()

    # ---- Analytics methods ----

    def insert_analytics(self, data: dict) -> int:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO analytics (video_id, platform, upload_date, title,
                   topic, category, script, duration_seconds, views, likes,
                   comments, shares, engagement_rate, retention_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.get("video_id"),
                    data.get("platform", ""),
                    data.get("upload_date"),
                    data.get("title", ""),
                    data.get("topic", ""),
                    data.get("category", ""),
                    data.get("script", ""),
                    data.get("duration_seconds", 0),
                    data.get("views", 0),
                    data.get("likes", 0),
                    data.get("comments", 0),
                    data.get("shares", 0),
                    data.get("engagement_rate", 0.0),
                    json.dumps(data.get("retention_data", {})),
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
        except sqlite3.Error as e:
            logger.error("Failed to insert analytics: %s", e)
            raise
        finally:
            conn.close()

    def get_analytics_for_period(
        self, since: datetime, until: Optional[datetime] = None
    ) -> list[dict]:
        conn = self._get_connection()
        try:
            if until is None:
                until = datetime.now()
            rows = conn.execute(
                """SELECT a.*, v.category as vid_category, v.title as vid_title,
                          v.duration_seconds as vid_duration, v.created_at
                   FROM analytics a
                   JOIN videos v ON a.video_id = v.id
                   WHERE a.upload_date >= ? AND a.upload_date <= ?
                   ORDER BY a.engagement_rate DESC""",
                (since.isoformat(), until.isoformat()),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_category_performance_30day(self) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """SELECT a.category,
                          COUNT(*) as total_videos,
                          COALESCE(SUM(a.views), 0) as total_views,
                          COALESCE(SUM(a.likes), 0) as total_likes,
                          COALESCE(SUM(a.comments), 0) as total_comments,
                          COALESCE(SUM(a.shares), 0) as total_shares,
                          COALESCE(AVG(a.engagement_rate), 0) as avg_engagement
                   FROM analytics a
                   WHERE a.upload_date >= ?
                   GROUP BY a.category
                   ORDER BY avg_engagement DESC""",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_analytics_by_category(self, category: str) -> list[dict]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM analytics WHERE category = ? ORDER BY upload_date DESC",
                (category,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- Category weights ----

    def get_category_weights(self) -> list[dict]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM category_weights ORDER BY weight DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_category_weight(
        self, category: str, weight: float, avg_engagement: float = 0.0
    ) -> None:
        conn = self._get_connection()
        try:
            conn.execute(
                """UPDATE category_weights
                   SET weight = ?, rolling_30day_avg = ?, last_updated = ?
                   WHERE category = ?""",
                (weight, avg_engagement, datetime.now().isoformat(), category),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to update category weight: %s", e)
        finally:
            conn.close()

    def get_category_weight(self, category: str) -> float:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT weight FROM category_weights WHERE category = ?",
                (category,),
            ).fetchone()
            return row["weight"] if row else 1.0
        finally:
            conn.close()

    def update_category_stats(
        self, category: str, views: int, likes: int
    ) -> None:
        conn = self._get_connection()
        try:
            conn.execute(
                """UPDATE category_weights
                   SET total_views = total_views + ?,
                       total_likes = total_likes + ?,
                       total_uploads = total_uploads + 1,
                       last_updated = ?
                   WHERE category = ?""",
                (views, likes, datetime.now().isoformat(), category),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to update category stats: %s", e)
        finally:
            conn.close()

    # ---- Reports ----

    def save_strategy_report(
        self, report_path: str, content: str, recommendations: str
    ) -> int:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO strategy_reports (report_path, content, recommendations)
                   VALUES (?, ?, ?)""",
                (report_path, content, recommendations),
            )
            conn.commit()
            return cur.lastrowid or 0
        except sqlite3.Error as e:
            logger.error("Failed to save strategy report: %s", e)
            raise
        finally:
            conn.close()

    def save_cleanup_report(
        self, report_path: str, files_deleted: int, space_recovered_mb: float, content: str
    ) -> int:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO cleanup_reports (report_path, files_deleted, space_recovered_mb, content)
                   VALUES (?, ?, ?, ?)""",
                (report_path, files_deleted, space_recovered_mb, content),
            )
            conn.commit()
            return cur.lastrowid or 0
        finally:
            conn.close()

    def get_last_strategy_report(self) -> Optional[dict]:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM strategy_reports ORDER BY report_date DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ---- Source Topics ----

    def insert_source_topic(self, item: dict) -> int:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO source_topics
                   (source, source_name, title, topic, category, url, score, rank_score, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.get("source", ""),
                    item.get("source_name", ""),
                    item.get("title", ""),
                    item.get("topic", ""),
                    item.get("category", ""),
                    item.get("url", ""),
                    float(item.get("score", 0) or 0),
                    float(item.get("rank_score", 0) or 0),
                    item.get("fetched_at", datetime.now().isoformat()),
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
        except sqlite3.Error as e:
            logger.error("Failed to insert source topic: %s", e)
            return 0
        finally:
            conn.close()

    def get_recent_source_topics(self, limit: int = 50) -> list[dict]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM source_topics WHERE used = 0 ORDER BY rank_score DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_source_topic_used(self, topic_id: int) -> None:
        conn = self._get_connection()
        try:
            conn.execute("UPDATE source_topics SET used = 1 WHERE id = ?", (topic_id,))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    # ---- Topic Candidates ----

    def insert_topic_candidate(
        self, topic: str, category: str, hook: str, angle: str,
        scores: dict, overall: float, selected: bool, source_inspiration: str = "",
    ) -> int:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO topic_candidates
                   (topic, category, hook, angle,
                    score_curiosity, score_novelty, score_educational, score_viral,
                    overall, selected, source_inspiration)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic, category, hook, angle,
                    scores.get("curiosity", 0), scores.get("novelty", 0),
                    scores.get("educational_value", 0), scores.get("viral_potential", 0),
                    overall, 1 if selected else 0, source_inspiration,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
        except sqlite3.Error as e:
            logger.error("Failed to insert topic candidate: %s", e)
            return 0
        finally:
            conn.close()

    def get_rejected_candidates(self, limit: int = 20) -> list[dict]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM topic_candidates WHERE selected = 0 ORDER BY overall DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- Quality Scores ----

    def save_quality_score(self, topic: str, category: str, result: dict) -> int:
        conn = self._get_connection()
        try:
            s = result.get("scores", {})
            cur = conn.execute(
                """INSERT INTO quality_scores
                   (topic, category, title, script_length,
                    score_hook, score_educational, score_originality,
                    score_engagement, score_clarity, overall, passed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic, category, result.get("title", ""),
                    result.get("script_length", 0),
                    s.get("hook", 0), s.get("educational_value", 0),
                    s.get("originality", 0), s.get("engagement_potential", 0),
                    s.get("clarity", 0), result.get("overall", 0),
                    1 if result.get("passed") else 0,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
        except sqlite3.Error as e:
            logger.error("Failed to save quality score: %s", e)
            return 0
        finally:
            conn.close()

    def get_recent_quality_scores(self, limit: int = 20) -> list[dict]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM quality_scores ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def close(self) -> None:
        pass
