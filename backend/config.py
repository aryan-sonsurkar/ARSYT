from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Local ClipFree paths and service settings."""

    root: Path = Path(__file__).resolve().parent.parent

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("CLIPFREE_DATA_DIR", self.root / "clipfree_data")).resolve()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "database" / "clipfree.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def ollama_url(self) -> str:
        return os.environ.get("CLIPFREE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

    @property
    def host(self) -> str:
        return os.environ.get("CLIPFREE_HOST", "127.0.0.1")

    @property
    def port(self) -> int:
        return int(os.environ.get("CLIPFREE_PORT", "8026"))

    @property
    def upload_chunk_bytes(self) -> int:
        return 1024 * 1024

    @property
    def max_upload_bytes(self) -> int:
        return int(os.environ.get("CLIPFREE_MAX_UPLOAD_BYTES", str(20 * 1024**3)))

    @property
    def minimum_free_bytes(self) -> int:
        return int(os.environ.get("CLIPFREE_MIN_FREE_BYTES", str(4 * 1024**3)))

    @property
    def whisper_models_dir(self) -> Path:
        return Path(os.environ.get("CLIPFREE_WHISPER_MODELS_DIR", self.data_dir / "models" / "whisper")).resolve()

    @property
    def default_whisper_model(self) -> str:
        return os.environ.get("CLIPFREE_WHISPER_MODEL", "small")

    @property
    def default_highlight_model(self) -> str:
        return os.environ.get("CLIPFREE_HIGHLIGHT_MODEL", "gemma3:4b")

    @property
    def default_clip_count(self) -> int:
        return int(os.environ.get("CLIPFREE_CLIP_COUNT", "5"))

    @property
    def default_min_clip_seconds(self) -> int:
        return int(os.environ.get("CLIPFREE_MIN_CLIP_SECONDS", "25"))

    @property
    def default_max_clip_seconds(self) -> int:
        return int(os.environ.get("CLIPFREE_MAX_CLIP_SECONDS", "60"))

    @property
    def default_render_preset(self) -> str:
        return os.environ.get("CLIPFREE_RENDER_PRESET", "podcast")

    @property
    def default_caption_style(self) -> str:
        return os.environ.get("CLIPFREE_CAPTION_STYLE", "kinetic")

    @property
    def render_concurrency(self) -> int:
        return max(1, int(os.environ.get("CLIPFREE_RENDER_CONCURRENCY", "1")))

    @property
    def export_dir(self) -> Path:
        return Path(os.environ.get("CLIPFREE_EXPORT_DIR", self.data_dir / "exports")).resolve()

    @property
    def ollama_request_timeout(self) -> float:
        return float(os.environ.get("CLIPFREE_OLLAMA_TIMEOUT", "180"))


settings = Settings()
