"""Instagram Reels publishing via the official Instagram Graph API.

Only uploads with the user's own Meta app credentials (instagram_client_secret.json
they supply themselves) and only after an explicit confirm request. Nothing is
published automatically and no credentials ship with ClipFree.

The Content Publish API makes Instagram fetch the video from a public URL, so
while a container is being created the rendered clip is served from this backend
through an origin reachable from Instagram — set CLIPFREE_INSTAGRAM_PUBLIC_URL
to a free tunnel (e.g. Cloudflare's cloudflared) that points back at this
server. The serve URL is random and only exposes files registered for the
upload window.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from backend.config import settings
from backend.database import connect, now_iso, row, rows
from backend.ingestion import IngestionError, JobCancelled

GRAPH = "https://graph.facebook.com/v21.0"
AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
TOKEN_URL = f"{GRAPH}/oauth/access_token"
OAUTH_SCOPE = "instagram_basic,instagram_content_publish,pages_show_list"
CONTAINER_TIMEOUT_SECONDS = 300.0
CONTAINER_POLL_SECONDS = 5.0
SERVE_TTL_SECONDS = 3600
MAX_CAPTION_CHARS = 2200

_client_file = "instagram_client_secret.json"
_token_file = "instagram_token.json"
_serve_file = "instagram_serve.json"


def _publish_dir() -> Path:
    directory = settings.data_dir / "publishing"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def client_secret_path() -> Path:
    return _publish_dir() / _client_file


def token_path() -> Path:
    return _publish_dir() / _token_file


def _serve_registry_path() -> Path:
    return _publish_dir() / _serve_file


def redirect_uri() -> str:
    configured = (os.environ.get("CLIPFREE_INSTAGRAM_REDIRECT_URI") or "").strip()
    if configured:
        return configured.rstrip("/")
    return f"http://{settings.host}:{settings.port}/api/instagram/auth/callback"


def public_base_url() -> str:
    """Public origin that serves this backend while a container is created."""
    return (os.environ.get("CLIPFREE_INSTAGRAM_PUBLIC_URL") or "").rstrip("/")


def _read_client_credentials() -> tuple[str, str]:
    path = client_secret_path()
    if not path.is_file():
        raise IngestionError(
            "No Meta app configured. Save your own Instagram app client JSON to "
            f"{path} and try again."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise IngestionError("instagram_client_secret.json is not valid JSON.") from exc
    # Meta downloads nest under "web"/"app"; a hand-written file can be flat.
    for candidate in (payload, payload.get("web") if isinstance(payload, dict) else None,
                      payload.get("app") if isinstance(payload, dict) else None):
        if isinstance(candidate, dict) and candidate.get("client_id") and candidate.get("client_secret"):
            return str(candidate["client_id"]), str(candidate["client_secret"])
    raise IngestionError("instagram_client_secret.json must contain client_id and client_secret.")


def _load_serve_registry() -> dict:
    try:
        data = json.loads(_serve_registry_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_serve_registry(registry: dict) -> None:
    _serve_registry_path().write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _fresh(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        registered_at = float(entry.get("registered_at") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= time.time() - registered_at <= SERVE_TTL_SECONDS


def register_served_file(video_path: Path) -> str:
    """Expose one rendered clip under a random URL token for the upload window."""
    registry = _load_serve_registry()
    token = str(registry.get("token") or secrets.token_urlsafe(24))
    files = {
        name: entry
        for name, entry in (registry.get("files") or {}).items()
        if _fresh(entry)
    }
    files[video_path.name] = {"path": str(video_path), "registered_at": time.time()}
    _write_serve_registry({"token": token, "files": files})
    return token


def public_clip_url(video_path: Path) -> str:
    token = register_served_file(video_path)
    return f"{public_base_url()}/api/instagram/serve/{token}?file={quote(video_path.name)}"


def resolve_served_file(token: str, name: str) -> Path | None:
    """Map a public serve URL back to a registered clip — never arbitrary paths."""
    if not token or not name or Path(name).name != name:
        return None
    registry = _load_serve_registry()
    if not token or token != registry.get("token"):
        return None
    entry = (registry.get("files") or {}).get(name)
    if not _fresh(entry):
        return None
    path = Path(str(entry.get("path") or ""))
    if path.is_file() and path.suffix.lower() in {".mp4", ".mov"}:
        return path
    return None


def _request_json(method: str, url: str, **kwargs: Any) -> dict:
    """Call the Graph API and surface Meta's error payloads as IngestionError."""
    try:
        response = httpx.request(method, url, timeout=60.0, **kwargs)
    except httpx.HTTPError as exc:
        raise IngestionError(f"Could not reach Instagram: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    error = payload.get("error")
    if response.status_code >= 400 or error:
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        raise IngestionError(f"Instagram API error: {message or response.text[:200] or response.status_code}")
    return payload


@dataclass
class InstagramPublishRequest:
    caption: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InstagramPublishRequest":
        return cls(caption=str(payload.get("caption") or "")[:MAX_CAPTION_CHARS])


class InstagramService:
    """Queued Instagram Reel posts that require an explicit user confirmation."""

    def __init__(self, jobs: Any) -> None:
        self.jobs = jobs
        self._oauth_states: dict[str, dict[str, str]] = {}

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        token = self._load_token()
        return {
            "client_configured": client_secret_path().is_file(),
            "authorized": bool(token),
            "username": str((token or {}).get("username") or ""),
            "token_expiry": str((token or {}).get("expires_at") or ""),
            "client_secret_path": str(client_secret_path()),
            "public_url": public_base_url(),
            "public_url_configured": bool(public_base_url()),
        }

    # -- OAuth ---------------------------------------------------------------
    def authorization_url(self) -> str:
        client_id, _ = _read_client_credentials()
        state = uuid.uuid4().hex
        self._oauth_states[state] = {"client_id": client_id}
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri(),
            "scope": OAUTH_SCOPE,
            "response_type": "code",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, state: str, code: str) -> dict:
        entry = self._oauth_states.pop(state, None)
        if entry is None:
            raise IngestionError("Unknown OAuth state. Start the connection again.")
        if not code:
            raise IngestionError("Instagram did not return an authorization code.")
        client_id, client_secret = _read_client_credentials()
        short = _request_json("GET", TOKEN_URL, params={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(),
            "code": code,
        })
        short_token = str(short.get("access_token") or "")
        if not short_token:
            raise IngestionError("Instagram did not return an access token.")
        long_lived = _request_json("GET", TOKEN_URL, params={
            "grant_type": "fb_exchange_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "fb_exchange_token": short_token,
        })
        user_token = str(long_lived.get("access_token") or short_token)
        pages = _request_json("GET", f"{GRAPH}/me/accounts", params={
            "fields": "id,name,access_token,instagram_business_account",
            "access_token": user_token,
        })
        ig_user_id, page_id, username = "", "", ""
        for page in pages.get("data") or []:
            linked = page.get("instagram_business_account") or {}
            if linked.get("id"):
                ig_user_id = str(linked["id"])
                page_id = str(page.get("id") or "")
                username = str(linked.get("username") or "")
                break
        if not ig_user_id:
            raise IngestionError(
                "No Instagram professional account is linked to a Facebook Page you manage. "
                "Convert the Instagram account to Business or Creator and link it to a Page "
                "(Meta Accounts Center), then connect again."
            )
        if not username:
            try:
                profile = _request_json("GET", f"{GRAPH}/{ig_user_id}", params={
                    "fields": "username", "access_token": user_token,
                })
                username = str(profile.get("username") or "")
            except IngestionError:
                username = ""
        expires_in = long_lived.get("expires_in")
        expires_at = ""
        try:
            if expires_in:
                import datetime as _dt

                moment = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=int(expires_in))
                expires_at = moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            expires_at = ""
        token_path().write_text(json.dumps({
            "access_token": user_token,
            "ig_user_id": ig_user_id,
            "page_id": page_id,
            "username": username,
            "expires_at": expires_at,
            "granted_at": now_iso(),
        }, indent=2), encoding="utf-8")
        return {"authorized": True, "username": username}

    def disconnect(self) -> None:
        token_path().unlink(missing_ok=True)

    # -- queueing ------------------------------------------------------------
    def start_publish(self, clip_id: str, request: InstagramPublishRequest, confirm: bool) -> dict:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            raise IngestionError("Clip not found.")
        if not confirm:
            raise IngestionError(
                "Publishing needs an explicit confirmation. Repeat with confirm=true after reviewing the video."
            )
        if self._load_token() is None:
            raise IngestionError(
                "Not authorized yet. Connect your own Instagram account under Publishing first."
            )
        if not public_base_url():
            raise IngestionError(
                "CLIPFREE_INSTAGRAM_PUBLIC_URL is not set. Instagram fetches the video from a "
                "public URL, so start a free tunnel (e.g. Cloudflare cloudflared) pointing at "
                "this backend and set that variable to its https:// address."
            )
        output = Path(clip.get("output_path") or "")
        if not output.is_file():
            raise IngestionError("Render this clip before publishing it.")
        if self._active_publish_jobs(clip_id):
            raise IngestionError("This clip already has an Instagram post queued or uploading.")
        caption = (request.caption or "").strip()[:MAX_CAPTION_CHARS] or self._default_caption(clip)
        job = self.jobs.create(clip["project_id"], "publish_instagram")
        payload = {
            "clip_id": clip_id,
            "caption": caption,
            "video_path": str(output),
        }
        self.jobs.update(job["id"], payload_json=json.dumps(payload))
        self.jobs.register_task(self.run(job["id"], clip_id, payload))
        return self.jobs.get(job["id"])

    async def retry(self, job_id: str) -> dict:
        previous = self.jobs.get(job_id)
        try:
            payload = json.loads(previous.get("payload_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise IngestionError("The previous Instagram settings could not be recovered.") from exc
        clip_id = payload.get("clip_id")
        if not clip_id:
            raise IngestionError("The previous Instagram job has no clip.")
        return self.start_publish(clip_id, InstagramPublishRequest.from_payload(payload), confirm=True)

    # -- execution -------------------------------------------------------------
    async def run(self, job_id: str, clip_id: str, payload: dict) -> None:
        video_path = Path(payload.get("video_path") or "")
        try:
            if not video_path.is_file():
                raise IngestionError("The rendered video is missing. Render the clip again.")
            token = self._load_token()
            if not token:
                raise IngestionError(
                    "Instagram authorization expired. Reconnect your account and publish again."
                )
            if not public_base_url():
                raise IngestionError(
                    "CLIPFREE_INSTAGRAM_PUBLIC_URL is not set, so Instagram cannot fetch the video."
                )
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Publish cancelled by user.")
            self.jobs.update(job_id, state="running", stage="connecting to Instagram", progress=5,
                             started_at=now_iso(), error="")

            def _upload() -> dict:
                return self._publish_reel(job_id, payload, video_path, token)

            result = await asyncio.to_thread(_upload)
            media_id = str(result.get("id") or "")
            permalink = str(result.get("permalink") or "")
            self.jobs.update(job_id, state="completed", stage="published", progress=100,
                             completed_at=now_iso(), cancel_requested=0,
                             payload_json=json.dumps({**payload, "instagram_media_id": media_id,
                                                      "instagram_permalink": permalink}))
            clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
            try:
                metadata = json.loads(clip.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            metadata["instagram_media_id"] = media_id
            if permalink:
                metadata["instagram_permalink"] = permalink
            with connect() as conn:
                conn.execute("UPDATE clips SET metadata_json=?,updated_at=? WHERE id=?",
                             (json.dumps(metadata), now_iso(), clip_id))
        except (IngestionError, JobCancelled) as exc:
            self._fail(job_id, str(exc), cancelled=True)
        except Exception as exc:
            self._fail(job_id, f"Instagram publish failed: {exc}")

    def _publish_reel(self, job_id: str, payload: dict, video_path: Path, token: dict) -> dict:
        ig_user_id = str(token.get("ig_user_id") or "")
        access_token = str(token.get("access_token") or "")
        if not ig_user_id or not access_token:
            raise IngestionError("Instagram credentials are incomplete. Reconnect your account.")
        video_url = public_clip_url(video_path)
        caption = str(payload.get("caption") or "")
        self.jobs.update(job_id, stage="handing the video to Instagram", progress=15)
        container = _request_json("POST", f"{GRAPH}/{ig_user_id}/media", data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": access_token,
        })
        container_id = str(container.get("id") or "")
        if not container_id:
            raise IngestionError("Instagram did not create a media container.")
        # Instagram downloads and processes the video before it can be published.
        deadline = time.monotonic() + CONTAINER_TIMEOUT_SECONDS
        last_stage = ""
        while True:
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Publish cancelled by user.")
            info = _request_json("GET", f"{GRAPH}/{container_id}", params={
                "fields": "status_code,status", "access_token": access_token,
            })
            status_code = str(info.get("status_code") or "")
            status_text = str(info.get("status") or "")
            if status_code == "FINISHED":
                break
            if status_code in {"ERROR", "EXPIRED"}:
                raise IngestionError(
                    f"Instagram could not process the video: {status_text or status_code}"
                )
            stage = f"Instagram processing: {status_text or status_code or 'waiting'}"
            if stage != last_stage:
                last_stage = stage
                self.jobs.update(job_id, stage=stage, progress=45)
            if time.monotonic() >= deadline:
                raise IngestionError(
                    "Instagram timed out processing the video. Try again in a few minutes."
                )
            time.sleep(CONTAINER_POLL_SECONDS)
        self.jobs.update(job_id, stage="publishing the Reel", progress=80)
        published = _request_json("POST", f"{GRAPH}/{ig_user_id}/media_publish", data={
            "creation_id": container_id,
            "access_token": access_token,
        })
        media_id = str(published.get("id") or "")
        if not media_id:
            raise IngestionError("Instagram accepted the video but did not return a media id.")
        permalink = ""
        try:
            info = _request_json("GET", f"{GRAPH}/{media_id}", params={
                "fields": "permalink", "access_token": access_token,
            })
            permalink = str(info.get("permalink") or "")
        except IngestionError:
            permalink = ""
        return {"id": media_id, "permalink": permalink}

    # -- helpers ----------------------------------------------------------------
    def _load_token(self) -> dict | None:
        if not token_path().is_file():
            return None
        try:
            payload = json.loads(token_path().read_text(encoding="utf-8"))
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            return None
        return payload

    def _active_publish_jobs(self, clip_id: str) -> list[dict]:
        active = rows(
            "SELECT * FROM jobs WHERE kind='publish_instagram' AND state IN ('queued','running')", ()
        )
        result = []
        for job in active:
            try:
                payload = json.loads(job.get("payload_json") or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("clip_id") == clip_id:
                result.append(job)
        return result

    def _default_caption(self, clip: dict) -> str:
        try:
            metadata = json.loads(clip.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        parts = [str(clip.get("title") or "").strip() or "Podcast Short"]
        hashtags = []
        for tag in metadata.get("hashtags") or []:
            text = str(tag).strip()
            if text:
                hashtags.append(text if text.startswith("#") else f"#{text.lstrip('#')}")
        if hashtags:
            parts.append(" ".join(hashtags[:10]))
        return "\n\n".join(parts)[:MAX_CAPTION_CHARS]

    def _fail(self, job_id: str, error: str, cancelled: bool = False) -> None:
        # Clip state is owned by the YouTube publish flow — only the job records
        # the Instagram outcome, so a failed Reel never clobbers a published clip.
        self.jobs.update(
            job_id,
            state="cancelled" if cancelled else "failed",
            stage="cancelled" if cancelled else "failed",
            error=error, completed_at=now_iso(),
        )


def recover_interrupted_instagram_publishes() -> None:
    """Mark Instagram jobs that were mid-flight when the backend stopped as failed."""
    active = rows(
        "SELECT * FROM jobs WHERE kind='publish_instagram' AND state IN ('queued','running')", ()
    )
    for job in active:
        with connect() as conn:
            conn.execute(
                "UPDATE jobs SET state='failed',stage='failed',"
                "error='The backend restarted during upload. Retry to publish again.',"
                "completed_at=? WHERE id=?",
                (now_iso(), job["id"]),
            )
