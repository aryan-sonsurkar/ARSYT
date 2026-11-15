from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    source_url: str = Field(default="", max_length=2048)

    @field_validator("source_url")
    @classmethod
    def local_http_source_only(cls, value: str) -> str:
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("Source URL must start with http:// or https://")
        return value


class JobCreate(BaseModel):
    """Creates an ingestion job before a streamed local upload."""

    kind: str = Field(default="ingestion", pattern="^ingestion$")


class UrlIngestRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    url: HttpUrl
    authorized: bool = Field(
        description="The caller confirms they own or are allowed to process this direct media URL."
    )


class TranscriptionStartRequest(BaseModel):
    model: str = Field(default="base", pattern="^(tiny|base|small|medium)$")
    language: str | None = Field(default=None, max_length=12)
    word_timestamps: bool = True
    device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")


class WhisperDownloadRequest(BaseModel):
    confirm_download: bool = False


class HighlightStartRequest(BaseModel):
    model: str = Field(default="", max_length=120)
    count: int = Field(default=5, ge=1, le=20)
    min_duration: float = Field(default=25, ge=8, le=120)
    max_duration: float = Field(default=60, ge=12, le=180)

    @field_validator("max_duration")
    @classmethod
    def duration_order(cls, value: float, info) -> float:
        minimum = info.data.get("min_duration", 25)
        if value <= minimum:
            raise ValueError("max_duration must be greater than min_duration")
        return value


class CandidateUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    hook: str | None = Field(default=None, max_length=240)
    reason: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=60)
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, ge=0)


class ClipCreateRequest(BaseModel):
    candidate_id: str | None = None
    title: str | None = Field(default=None, max_length=120)
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, ge=0)


class ClipUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=140)
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, ge=0)
    preset: str | None = Field(default=None, pattern="^(cinematic|podcast|minimal|gaming|motivational)$")
    caption_style: str | None = Field(default=None, pattern="^(kinetic|word|cinematic|podcast|minimal)$")
    music_path: str | None = None
    music_volume: float | None = Field(default=None, ge=0, le=1)
    caption_options: dict | None = None


class SocialCaptionRequest(BaseModel):
    model: str = Field(default="", max_length=120)
    count: int = Field(default=5, ge=2, le=5)


class SocialVariant(BaseModel):
    style: str = Field(default="", max_length=40)
    label: str = Field(default="", max_length=60)
    hook: str = Field(default="", max_length=200)
    caption: str = Field(default="", max_length=1200)
    hashtags: list[str] = Field(default_factory=list)
    cta: str = Field(default="", max_length=200)


class SocialSaveRequest(BaseModel):
    variants: list[SocialVariant] = Field(default_factory=list)


class RenderStartRequest(BaseModel):
    preset: str = Field(default="podcast", pattern="^(cinematic|podcast|minimal|gaming|motivational)$")
    caption_style: str = Field(default="", max_length=40)
    caption_options: dict = Field(default_factory=dict)
    music_path: str = Field(default="", max_length=500)
    music_volume: float = Field(default=0.18, ge=0, le=1)
    fps: int = Field(default=30, ge=15, le=60)
    ducking: bool = True
    metadata_model: str = Field(default="", max_length=120)
    smart_reframe: bool = True
    trim_silence: bool = False


class BatchClipRequest(BaseModel):
    clip_ids: list[str] = Field(min_length=1, max_length=100)

class BatchRenderRequest(BatchClipRequest):
    options: RenderStartRequest = Field(default_factory=RenderStartRequest)


class PipelineStartRequest(BaseModel):
    """Runs transcription, highlights, approval, rendering and export in one job."""

    transcription_model: str = Field(default="", pattern="^(|tiny|base|small|medium)$")
    language: str | None = Field(default=None, max_length=12)
    device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")
    highlight_model: str = Field(default="", max_length=120)
    count: int = Field(default=5, ge=1, le=20)
    min_duration: float = Field(default=25, ge=8, le=120)
    max_duration: float = Field(default=60, ge=12, le=180)
    render: RenderStartRequest = Field(default_factory=RenderStartRequest)

    @field_validator("max_duration")
    @classmethod
    def duration_order(cls, value: float, info) -> float:
        minimum = info.data.get("min_duration", 25)
        if value <= minimum:
            raise ValueError("max_duration must be greater than min_duration")
        return value


class BatchCandidateRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)


class SettingsUpdate(BaseModel):
    highlight_model: str | None = Field(default=None, max_length=120)
    clip_count: int | None = Field(default=None, ge=1, le=20)
    min_clip_seconds: float | None = Field(default=None, ge=8, le=120)
    max_clip_seconds: float | None = Field(default=None, ge=12, le=180)
    render_preset: str | None = Field(default=None, max_length=40)
    caption_style: str | None = Field(default=None, max_length=40)
    metadata_model: str | None = Field(default=None, max_length=120)
    background_music_volume: float | None = Field(default=None, ge=0, le=1)
    smart_reframe: bool | None = None
    export_dir: str | None = Field(default=None, max_length=500)
