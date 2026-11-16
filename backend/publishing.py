"""YouTube publish queue via the official Data API v3.

Only uploads with the user's own Google Cloud OAuth client (client_secret.json
they supply themselves) and only after an explicit confirm request. Nothing is
published automatically and no credentials ship with ClipFree.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from backend.config import settings
from backend.database import connect, now_iso, row
from backend.ingestion import IngestionError, JobCancelled

OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT_URI = "http://127.0.0.1:8026/api/publishing/auth/callback"
CATEGORY_IDS = {"people": "22", "gaming": "20", "education": "27", "entertainment": "24"}
PRIVACY_STATES = ("public", "unlisted", "private")

_client_file = "client_secret.json"
_token_file = "token.json"


def _publish_dir() -> Path:
    directory = settings.data_dir / "publishing"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def client_secret_path() -> Path:
    return _publish_dir() / _client_file


def token_path() -> Path:
    return _publish_dir() / _token_file


@dataclass
class PublishRequest:
    title: str = ""
    description: str = ""
    tags: list[str] | None = None
    privacy: str = "public"
    category: str = "entertainment"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PublishRequest":
        privacy = str(payload.get("privacy") or "public")
        if privacy not in PRIVACY_STATES:
            raise IngestionError("Privacy must be one of: public, unlisted, private.")
        category = str(payload.get("category") or "entertainment")
        if category not in CATEGORY_IDS:
            raise IngestionError("Unknown category. Use one of: " + ", ".join(CATEGORY_IDS))
        tags = [str(tag).strip() for tag in (payload.get("tags") or []) if str(tag).strip()]
        return cls(
            title=str(payload.get("title") or "")[:100],
            description=str(payload.get("description") or "")[:5000],
            tags=tags[:30],
            privacy=privacy,
            category=category,
        )


class PublishingService:
    """Queued YouTube uploads that require an explicit user confirmation."""

    def __init__(self, jobs: Any) -> None:
        self.jobs = jobs
        self._oauth_states: dict[str, str] = {}

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        authorized = False
        expires = ""
        try:
            authorized = self._load_credentials() is not None
        except Exception:
            authorized = False
        try:
            token = json.loads(token_path().read_text(encoding="utf-8"))
            expires = str(token.get("expiry") or "")
        except Exception:
            pass
        return {
            "client_configured": client_secret_path().is_file(),
            "authorized": authorized,
            "token_expiry": expires,
            "client_secret_path": str(client_secret_path()),
        }

    # -- OAuth ---------------------------------------------------------------
    def authorization_url(self) -> str:
        if not client_secret_path().is_file():
            raise IngestionError(
                "No OAuth client configured. Save your own Google Cloud client_secret.json to "
                f"{client_secret_path()} and try again."
            )
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise IngestionError("The Google API client libraries are not installed.") from exc
        state = uuid.uuid4().hex
        flow = Flow.from_client_secrets_file(str(client_secret_path()), scopes=[OAUTH_SCOPE])
        flow.redirect_uri = REDIRECT_URI
        url, self._oauth_states[state] = flow.authorization_url(
            access_type="offline", include_granted_scopes="false", prompt="consent", state=state
        ), state
        return url

    def exchange_code(self, state: str, code: str) -> dict:
        if state not in self._oauth_states:
            raise IngestionError("Unknown OAuth state. Start the connection again.")
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:  # pragma: no cover
            raise IngestionError("The Google API client libraries are not installed.") from exc
        self._oauth_states.pop(state, None)
        flow = Flow.from_client_secrets_file(str(client_secret_path()), scopes=[OAUTH_SCOPE], state=state)
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)
        credentials = flow.credentials
        token_payload = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes or []),
            "expiry": credentials.expiry.isoformat() if credentials.expiry else "",
        }
        token_path().write_text(json.dumps(token_payload, indent=2), encoding="utf-8")
        return {"authorized": True, "token_expiry": token_payload["expiry"]}

    def disconnect(self) -> None:
        token_path().unlink(missing_ok=True)

    # -- credentials ---------------------------------------------------------
    def _load_credentials(self) -> Any | None:
        if not token_path().is_file():
            return None
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError:  # pragma: no cover
            return None
        payload = json.loads(token_path().read_text(encoding="utf-8"))
        credentials = Credentials(
            token=payload.get("token"),
            refresh_token=payload.get("refresh_token"),
            token_uri=payload.get("token_uri"),
            client_id=payload.get("client_id"),
            client_secret=payload.get("client_secret"),
            scopes=payload.get("scopes") or [OAUTH_SCOPE],
        )
        if credentials.valid:
            return credentials
        if not credentials.refresh_token:
            return None
        try:
            credentials.refresh(Request())
        except Exception:
            return None
        token_path().write_text(json.dumps({
            **payload,
            "token": credentials.token,
            "expiry": credentials.expiry.isoformat() if credentials.expiry else "",
        }, indent=2), encoding="utf-8")
        return credentials

    # -- queueing ------------------------------------------------------------
    def start_publish(self, clip_id: str, request: PublishRequest, confirm: bool) -> dict:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            raise IngestionError("Clip not found.")
        if not confirm:
            raise IngestionError(
                "Publishing needs an explicit confirmation. Repeat with confirm=true after reviewing the video."
            )
        if self._load_credentials() is None:
            raise IngestionError(
                "Not authorized yet. Connect your own YouTube account under Publishing first."
            )
        output = Path(clip.get("output_path") or "")
        if not output.is_file():
            raise IngestionError("Render this clip before publishing it.")
        if self._active_publish_jobs(clip_id):
            raise IngestionError("This clip is already queued or uploading.")
        title = (request.title or str(clip.get("title") or ""))[:100].strip()
        if not title:
            raise IngestionError("A title is required to publish a Short.")
        description = request.description or ""
        hashtags = self._clip_hashtags(clip)
        if hashtags and "#shorts" not in [tag.lower() for tag in hashtags]:
            hashtags = hashtags + ["#Shorts"]
        if hashtags:
            description = (description + "\n\n" + " ".join(hashtags)).strip()
        job = self.jobs.create(clip["project_id"], "publish")
        payload = {
            "clip_id": clip_id,
            "title": title,
            "description": description[:5000],
            "tags": (request.tags or [])[:30],
            "privacy": request.privacy,
            "category_id": CATEGORY_IDS[request.category],
            "video_path": str(output),
        }
        self.jobs.update(job["id"], payload_json=json.dumps(payload))
        with connect() as conn:
            conn.execute("UPDATE clips SET state='publishing',error='',updated_at=? WHERE id=?", (now_iso(), clip_id))
        self.jobs.register_task(self.run(job["id"], clip_id, payload))
        return self.jobs.get(job["id"])

    async def retry(self, job_id: str) -> dict:
        previous = self.jobs.get(job_id)
        try:
            payload = json.loads(previous.get("payload_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise IngestionError("The previous publish settings could not be recovered.") from exc
        clip_id = payload.get("clip_id")
        if not clip_id:
            raise IngestionError("The previous publish job has no clip.")
        return self.start_publish(clip_id, PublishRequest.from_payload(payload), confirm=True)

    # -- execution -------------------------------------------------------------
    async def run(self, job_id: str, clip_id: str, payload: dict) -> None:
        video_path = Path(payload.get("video_path") or "")
        try:
            if not video_path.is_file():
                raise IngestionError("The rendered video is missing. Render the clip again.")
            if self._load_credentials() is None:
                raise IngestionError("YouTube authorization expired. Reconnect your account and publish again.")
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Publish cancelled by user.")
            self.jobs.update(job_id, state="running", stage="connecting to YouTube", progress=1, started_at=now_iso(), error="")

            def _upload() -> str:
                return self._resumable_upload(job_id, payload, video_path)

            video_id = await asyncio.to_thread(_upload)
            self.jobs.update(job_id, state="completed", stage="published", progress=100,
                             completed_at=now_iso(), cancel_requested=0,
                             payload_json=json.dumps({**payload, "youtube_video_id": video_id}))
            clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
            try:
                metadata = json.loads(clip.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            metadata["youtube_video_id"] = video_id
            metadata["youtube_url"] = f"https://youtube.com/shorts/{video_id}"
            with connect() as conn:
                conn.execute("UPDATE clips SET state='published',metadata_json=?,updated_at=? WHERE id=?",
                             (json.dumps(metadata), now_iso(), clip_id))
        except (IngestionError, JobCancelled) as exc:
            self._fail(clip_id, job_id, str(exc), cancelled=True)
        except Exception as exc:
            self._fail(clip_id, job_id, f"YouTube upload failed: {exc}")

    def _resumable_upload(self, job_id: str, payload: dict, video_path: Path) -> str:
        import io

        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        credentials = self._load_credentials()
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        body = {
            "snippet": {
                "title": payload.get("title") or "Podcast Short",
                "description": payload.get("description") or "",
                "tags": payload.get("tags") or [],
                "categoryId": payload.get("category_id") or "24",
            },
            "status": {
                "privacyStatus": payload.get("privacy") or "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        size = video_path.stat().st_size
        media = MediaIoBaseUpload(io.BytesIO(video_path.read_bytes()), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        last_fraction = 0.0
        while response is None:
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Publish cancelled by user.")
            status, response = request.next_chunk()
            if status is not None:
                fraction = float(status.get("progress") or 0.0)
                if fraction > last_fraction + 0.01:
                    last_fraction = fraction
                    self.jobs.update(job_id, progress=round(5 + fraction * 90, 2), stage="uploading to YouTube")
        video_id = str((response or {}).get("id") or "")
        if not video_id:
            raise IngestionError("YouTube did not return a video id.")
        return video_id

    # -- helpers ----------------------------------------------------------------
    def _active_publish_jobs(self, clip_id: str) -> list[dict]:
        from backend.database import rows

        active = rows("SELECT * FROM jobs WHERE kind='publish' AND state IN ('queued','running')", ())
        result = []
        for job in active:
            try:
                payload = json.loads(job.get("payload_json") or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("clip_id") == clip_id:
                result.append(job)
        return result

    def _clip_hashtags(self, clip: dict) -> list[str]:
        try:
            metadata = json.loads(clip.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        tags = []
        for tag in metadata.get("hashtags") or []:
            text = str(tag).strip()
            if not text:
                continue
            tags.append(text if text.startswith("#") else f"#{text.lstrip('#')}")
        return tags[:15]

    def _fail(self, clip_id: str, job_id: str, error: str, cancelled: bool = False) -> None:
        self.jobs.update(
            job_id,
            state="cancelled" if cancelled else "failed",
            stage="cancelled" if cancelled else "failed",
            error=error, completed_at=now_iso(),
        )
        with connect() as conn:
            conn.execute(
                "UPDATE clips SET state=?,error=?,updated_at=? WHERE id=?",
                ("draft" if cancelled else "failed", error, now_iso(), clip_id),
            )


def recover_interrupted_publishes() -> None:
    """Mark publish jobs that were mid-flight when the backend stopped as failed."""
    from backend.database import rows

    active = rows("SELECT * FROM jobs WHERE kind='publish' AND state IN ('queued','running')", ())
    for job in active:
        try:
            payload = json.loads(job.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        clip_id = str(payload.get("clip_id") or "")
        with connect() as conn:
            conn.execute(
                "UPDATE jobs SET state='failed',stage='failed',error='The backend restarted during upload. Retry to publish again.',completed_at=? WHERE id=?",
                (now_iso(), job["id"]),
            )
            if clip_id:
                conn.execute("UPDATE clips SET state='failed',error='Publish interrupted by a backend restart.',updated_at=? WHERE id=?", (now_iso(), clip_id))
