from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Project paths
    project_root: Path = Path(__file__).resolve().parent.parent
    logs_dir: Path = project_root / "logs"
    outputs_dir: Path = project_root / "outputs"
    scripts_dir: Path = project_root / "scripts"
    voices_dir: Path = project_root / "voices"
    images_dir: Path = project_root / "images"
    subtitles_dir: Path = project_root / "subtitles"
    uploads_dir: Path = project_root / "uploads"
    database_dir: Path = project_root / "database"
    reports_dir: Path = project_root / "reports"
    temp_dir: Path = project_root / "temp"
    cache_dir: Path = project_root / "cache"
    intermediate_dir: Path = project_root / "intermediate_renders"
    screenshots_dir: Path = logs_dir / "screenshots"

    # Ollama
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3"
    topic_model: str = "llama3.2:3b"
    content_model: str = "qwen3"
    script_model: str = "qwen3"
    ollama_timeout: int = 120

    # Kokoro TTS
    kokoro_model_path: str = ""
    kokoro_voice: str = "af_heart"

    # Visual Assets (internet images, no paid APIs)
    num_images: int = 10

    # YouTube (only needed for initial --setup-sessions)
    youtube_channel_id: str = ""

    # Schedule
    schedule_time: str = "08:00"

    # General
    log_level: str = "INFO"
    max_retries: int = 3
    retry_delay: int = 60
    background_music_path: str = ""
    approval_mode: bool = False
    quality_threshold: float = 6.0
    quality_max_retries: int = 3

    # Strategy Agent
    strategy_enabled: bool = True
    strategy_review_day: int = 6
    rolling_average_days: int = 30
    max_category_weight: float = 0.5
    min_category_weight: float = 0.05

    # Topic Sources
    topic_sources_enabled: bool = True

    # Browser Profiles (persistent sessions)
    browser_profiles_dir: Path = project_root / "browser_profiles"

    # Dedicated Chrome automation profile (NOT the user's Default profile).
    # Chrome 149+ blocks DevTools remote debugging on the primary profile
    # ("DevTools remote debugging requires a non-default data directory").
    #
    # CodeShortsBot creates and uses a separate profile to bypass this.
    # Leave empty to auto-create "CodeShortsBot" under the User Data dir.
    chrome_automation_profile_dir: str = ""

    # Legacy — no longer used.  Kept so existing .env files don't break.
    chrome_user_data_dir: str = ""
    chrome_profile_name: str = "Default"

    # Storage Management
    cleanup_enabled: bool = True
    cleanup_retention_days: int = 7
    storage_warning_threshold: int = 80
    free_storage_warning_gb: int = 100
    max_temp_size_mb: int = 1024

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for d in [
            self.logs_dir, self.outputs_dir, self.scripts_dir,
            self.voices_dir, self.images_dir, self.subtitles_dir,
            self.uploads_dir, self.database_dir, self.reports_dir,
            self.temp_dir, self.cache_dir, self.intermediate_dir,
            self.browser_profiles_dir, self.screenshots_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.database_dir / "codeshorts.db"

    @property
    def protected_dirs(self) -> list[str]:
        return [
            "outputs", "database", "reports", "config", "models",
            "browser_profiles",
        ]


@lru_cache()
def get_settings() -> Settings:
    return Settings()
