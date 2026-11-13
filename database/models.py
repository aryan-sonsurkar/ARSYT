from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Topic:
    id: Optional[int] = None
    topic: str = ""
    category: str = ""
    used_at: datetime = field(default_factory=datetime.now)

    @property
    def is_used(self) -> bool:
        return self.id is not None


@dataclass
class Video:
    id: Optional[int] = None
    topic_id: Optional[int] = None
    title: str = ""
    script: str = ""
    description: str = ""
    hashtags: str = ""
    video_path: str = ""
    voice_path: str = ""
    duration_seconds: float = 0.0
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    uploaded_youtube: bool = False
    upload_attempts: int = 0
    last_upload_error: str = ""
