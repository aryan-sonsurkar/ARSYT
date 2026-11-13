from __future__ import annotations

import logging
import os
import traceback
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import get_settings
from database.repository import Repository

logger = logging.getLogger(__name__)

# Anti-detection init script — still applied so Playwright's CDP
# connection doesn't leak automation fingerprints into the page JS.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) => (
  params.name === 'notifications'
    ? Promise.resolve({ state: 'denied' })
    : originalQuery(params)
);
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5],
});
Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en'],
});
"""

# Chrome launch args that suppress automation banners.
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-background-networking",
]

# Common install paths for Google Chrome on Windows.
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
if os.environ.get("LOCALAPPDATA"):
    _CHROME_CANDIDATES.append(
        str(
            Path(os.environ["LOCALAPPDATA"])
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        )
    )


def _detect_browser() -> dict:
    """Detect the best available browser and return its metadata."""
    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).exists():
            return {
                "path": candidate,
                "channel": "chrome",
                "browser_type": "Chrome",
                "engine": "chromium",
            }
    return {
        "path": None,
        "channel": None,
        "browser_type": "Chromium (bundled)",
        "engine": "chromium",
    }


def _default_user_data_dir() -> Optional[str]:
    """Return the default Chrome user data directory on Windows."""
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        path = Path(localappdata) / "Google" / "Chrome" / "User Data"
        if path.exists():
            return str(path)
    appdata = os.environ.get("APPDATA")
    if appdata:
        path = Path(appdata) / "Google" / "Chrome" / "User Data"
        if path.exists():
            return str(path)
    return None


def list_chrome_profiles() -> list[dict]:
    """Scan the Chrome user data directory and return detected profiles.

    Each entry has keys:
        name       – profile directory name (e.g. "Default", "Profile 1")
        display    – display name from Preferences (or the dir name)
        path       – full path to the profile directory
    """
    settings = get_settings()
    raw_dir = settings.chrome_user_data_dir
    if raw_dir:
        root = Path(raw_dir)
    else:
        detected = _default_user_data_dir()
        if detected:
            root = Path(detected)
        else:
            return []

    profiles: list[dict] = []
    # Known Chrome profile directory names.
    candidates = ["Default"] + [
        f"Profile {i}" for i in range(1, 50)
    ]

    for name in candidates:
        profile_path = root / name
        if not profile_path.is_dir():
            continue
        # A real profile has at least a Preferences or Cookies file.
        if not (
            (profile_path / "Preferences").exists()
            or (profile_path / "Cookies").exists()
            or (profile_path / "Network" / "Cookies").exists()
        ):
            continue
        # Try to read the display name from Preferences JSON.
        display = name
        prefs_path = profile_path / "Preferences"
        if prefs_path.exists():
            try:
                import json as _json
                prefs = _json.loads(prefs_path.read_text(encoding="utf-8"))
                display = (
                    prefs.get("profile", {})
                    .get("name", name)
                )
            except Exception:
                pass
        profiles.append({
            "name": name,
            "display": display,
            "path": str(profile_path),
        })

    return profiles


class SessionManager:
    """Uses a dedicated Chrome profile for authenticated YouTube sessions.

    Chrome 149+ blocks DevTools remote debugging on the user's primary
    profile (``Default``) with the error::

        DevTools remote debugging requires a non-default data directory.

    To work around this, CodeShortsBot creates and uses a **dedicated**
    automation profile (``CodeShortsBot``) that lives alongside the
    user's real profiles under the Chrome User Data directory.

    **Setup** (one-time)::

        1. Run ``python main.py --create-profile``
        2. Log into YouTube Studio in the opened Chrome window.
        3. Close Chrome.
        4. Run ``python main.py --verify-youtube``
        5. If PASS, you are done.

    Configuration (``.env``)::

        CHROME_AUTOMATION_PROFILE_DIR=C:\\Users\\<you>\\AppData\\Local\\Google\\Chrome\\User Data\\CodeShortsBot

    If left empty the default is ``<User Data>\\CodeShortsBot``.
    """

    def __init__(self, platform: str) -> None:
        self.settings = get_settings()
        self.platform = platform

        # Resolve the dedicated automation profile directory.
        raw_dir = self.settings.chrome_automation_profile_dir
        if raw_dir:
            self._user_data_dir = Path(raw_dir)
        else:
            detected = _default_user_data_dir()
            if detected:
                self._user_data_dir = Path(detected) / "CodeShortsBot"
            else:
                raise RuntimeError(
                    "CHROME_AUTOMATION_PROFILE_DIR is not set and the "
                    "default Chrome User Data directory was not found.  "
                    "Please run:\n"
                    "  python main.py --create-profile\n"
                    "to set up the automation profile."
                )

        # Chrome always creates a Default/ profile inside the user data dir.
        self.profile_dir = self._user_data_dir / "Default"

        # Detect browser executable.
        self._browser_info = _detect_browser()
        self._log_startup_info()

    # ── logging ──────────────────────────────────────────────────

    def _log_startup_info(self) -> None:
        info = self._browser_info
        logger.info(
            "SessionManager[%s]: "
            "engine=%s, "
            "browser_type=%s, "
            "channel=%s, "
            "executable_path=%s, "
            "user_data_dir=%s, "
            "profile_dir=%s, "
            "profile_exists=%s, "
            "launch_args=%s",
            self.platform,
            info["engine"],
            info["browser_type"],
            info["channel"],
            info["path"] or "(bundled)",
            self._user_data_dir,
            self.profile_dir,
            self.profile_dir.exists(),
            _STEALTH_ARGS,
        )

    # ── session lifecycle ────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """The profile is ready when its directory exists and contains
        Chrome profile data (e.g. the Cookies DB)."""
        if not self._user_data_dir.exists():
            return False
        return (
            (self.profile_dir / "Cookies").exists()
            or (self.profile_dir / "Network" / "Cookies").exists()
        )

    def mark_ready(self) -> None:
        pass  # No-op — the profile is managed by Chrome itself.

    def reset(self) -> None:
        logger.info(
            "reset() is a no-op for dedicated Chrome profiles. "
            "To reset, log out of the website in the automation profile."
        )

    # ── context creation ─────────────────────────────────────────

    async def get_context(self, p) -> BrowserContext:
        """Launch Chrome with the dedicated automation profile.

        The ``user_data_dir`` points directly to the CodeShortsBot
        directory (NOT the user's ``Default`` profile), so Chrome does
        **not** block DevTools remote debugging.
        """
        args = list(_STEALTH_ARGS)

        kwargs: dict = {
            "user_data_dir": str(self._user_data_dir),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "no_viewport": False,
            "args": args,
        }

        if self._browser_info["path"]:
            kwargs["channel"] = self._browser_info["channel"]
            kwargs["executable_path"] = self._browser_info["path"]

        context = await p.chromium.launch_persistent_context(**kwargs)
        logger.debug("get_context: type(context)=%s", type(context).__name__)
        await context.add_init_script(_STEALTH_INIT_SCRIPT)
        return context

    # ── session validation ───────────────────────────────────────

    async def validate_session(
        self, check_url: str, success_indicator: str
    ) -> bool:
        """Open the platform page and verify the session is still valid.

        Checks:
          1. The current URL stays on the expected domain (not
             redirected to a login page).
          2. A logged-in-only UI element (avatar, upload button, …)
             is visible on the page.

        Returns ``True`` if both checks pass.
        """
        if not self.is_ready:
            logger.warning(
                "Profile directory %s does not exist or has no "
                "profile data.  Make sure Chrome is installed and "
                "you have used it to browse at least once.",
                self.profile_dir,
            )
            return False

        try:
            async with async_playwright() as p:
                context = await self.get_context(p)

                # Use the first existing page (the visible one), not
                # context.new_page() which opens a background tab.
                page = context.pages[0] if context.pages else await context.new_page()

                try:
                    try:
                        await page.goto(
                            check_url,
                            timeout=60000,
                            wait_until="networkidle",
                        )
                    except PlaywrightTimeout:
                        pass

                    final_url = page.url
                    logger.info(
                        "Session check URL for %s: %s",
                        self.platform,
                        final_url,
                    )

                    if final_url in ("about:blank",):
                        logger.warning(
                            "Navigation to %s failed for %s — "
                            "page is still about:blank",
                            check_url,
                            self.platform,
                        )
                        return False

                    # 1) URL validation
                    expected_prefix = check_url.rstrip("/")
                    if not final_url.startswith(expected_prefix):
                        logger.warning(
                            "Session expired for %s: "
                            "redirected to %s (expected %s).",
                            self.platform,
                            final_url,
                            expected_prefix,
                        )
                        return False

                    # 2) Element validation
                    try:
                        await page.wait_for_selector(
                            success_indicator, timeout=5000
                        )
                    except PlaywrightTimeout:
                        logger.warning(
                            "Session expired for %s: "
                            "auth element '%s' not found.",
                            self.platform,
                            success_indicator,
                        )
                        return False

                    logger.info(
                        "Session valid for %s", self.platform
                    )
                    return True
                finally:
                    await context.close()
        except Exception as e:
            logger.warning(
                "Session validation failed for %s: %s",
                self.platform,
                e,
            )
            return False


class BaseUploader:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()

    async def _capture_debug_info(
        self,
        page: Optional[Page],
        platform: str,
        exception: Exception,
    ) -> None:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        base = self.settings.screenshots_dir / f"{ts}-{platform}"
        base.mkdir(parents=True, exist_ok=True)

        if page:
            try:
                await page.screenshot(path=str(base.with_suffix(".png")))
            except Exception as e:
                logger.warning("Screenshot failed: %s", e)

        if page:
            try:
                html = await page.content()
                (base.parent / f"{base.name}.html").write_text(
                    html, encoding="utf-8"
                )
            except Exception as e:
                logger.warning("HTML capture failed: %s", e)

        tb = traceback.format_exc()
        (base.parent / f"{base.name}-traceback.txt").write_text(
            tb, encoding="utf-8"
        )

        meta = (
            f"Platform: {platform}\n"
            f"Time: {datetime.now().isoformat()}\n"
            f"Exception: {type(exception).__name__}: {exception}\n"
        )
        (base.parent / f"{base.name}-metadata.txt").write_text(
            meta, encoding="utf-8"
        )

        logger.info("Debug info saved: %s", base.name)

    async def _get_persistent_context(
        self, p: async_playwright, session: SessionManager
    ) -> Optional[BrowserContext]:
        """Return a persistent context.

        Auth validation is done inline inside the upload method
        (waits for #avatar-btn after navigating to the dashboard),
        so we skip the separate validate_session call that would
        launch Chrome twice.
        """
        if not session.is_ready:
            logger.error(
                "Chrome profile not found at %s.  Make sure you have "
                "used Chrome at least once on this machine.",
                session.profile_dir,
            )
            return None

        return await session.get_context(p)


class YouTubeUploader(BaseUploader):
    """Uploads Shorts to YouTube Studio via the dashboard Create button.

    Uses deterministic locators (role → visible text → CSS fallback),
    logs every stage, and captures diagnostics on any failure.
    Never navigates to ``/upload`` directly.
    """

    def __init__(self) -> None:
        super().__init__()
        self.session = SessionManager("youtube")
        self._login_url = "https://studio.youtube.com"
        self._success_indicator = "#avatar-btn"
        self._platform_label = "YouTube"

    # ── public entry point ─────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=10, max=60),
    )
    async def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        hashtags: str,
    ) -> bool:
        if not video_path.exists():
            logger.error("Video not found: %s", video_path)
            return False

        full_description = f"{description}\n\n{hashtags}"

        logger.info("START upload: title='%s' path=%s", title, video_path)
        try:
            result = await self._upload_playwright(
                video_path, title, full_description
            )
            logger.info("END upload: title='%s' result=%s", title, result)
            return result
        except Exception as e:
            logger.error("YouTube upload failed after all retries: %s", e)
            return False

    # ── pre-flight check ───────────────────────────────────────────

    def _preflight_check(self, video_path: Path) -> None:
        """Check file exists, has content, and meets Shorts specs."""
        import json
        import subprocess as _sp

        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        size = video_path.stat().st_size
        if size == 0:
            raise RuntimeError(f"Video file is empty (0 bytes): {video_path}")

        logger.info("Preflight: file=%s size=%d bytes", video_path.name, size)

        try:
            result = _sp.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "stream=width,height,duration,codec_type",
                    "-of", "json",
                    str(video_path),
                ],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(result.stdout)
        except Exception as e:
            logger.warning("Preflight ffprobe failed: %s", e)
            return

        duration: Optional[float] = None
        width: Optional[int] = None
        height: Optional[int] = None

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = stream.get("width") or width
                height = stream.get("height") or height
                if stream.get("duration"):
                    duration = float(stream["duration"])

        if duration is None:
            duration = float(data.get("format", {}).get("duration", 0))

        dur_ok = duration > 0 and duration <= 60
        res_ok = width == 1080 and height == 1920

        logger.info(
            "Short specs: duration=%.1fs (%s) resolution=%dx%d (%s)",
            duration, "PASS" if dur_ok else "FAIL",
            width or 0, height or 0, "PASS" if res_ok else "FAIL",
        )

        if not dur_ok:
            logger.warning("Duration %.1fs exceeds 60s short limit", duration)
        if not res_ok:
            logger.warning(
                "Resolution %dx%d differs from expected 1080x1920", width, height
            )

    # ── stage helpers ──────────────────────────────────────────────

    async def _log_url(self, page: Page, label: str) -> None:
        try:
            url = page.url
            title = await page.title()
            logger.info("[%s] URL=%s  title=%s", label, url, title)
        except Exception:
            pass

    async def _screenshot(self, page: Page, name: str) -> None:
        try:
            path = self.settings.screenshots_dir / name
            await page.screenshot(path=str(path))
            logger.info("Screenshot saved: %s", name)
        except Exception as e:
            logger.warning("Screenshot failed [%s]: %s", name, e)

    async def _stage_fail(
        self, page: Page, stage: str, exc: Exception
    ) -> None:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        base = self.settings.screenshots_dir / f"{ts}-youtube-stage-{stage}"
        base.mkdir(parents=True, exist_ok=True)

        try:
            await page.screenshot(path=str(base.with_suffix(".png")))
        except Exception:
            pass
        try:
            html = await page.content()
            (base.parent / f"{base.name}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass
        try:
            page_title = await page.title()
            meta = (
                f"Stage: {stage}\n"
                f"URL: {page.url}\n"
                f"Page title: {page_title}\n"
                f"Time: {datetime.now().isoformat()}\n"
                f"Exception: {type(exc).__name__}: {exc}\n"
                f"Stack:\n{traceback.format_exc()}\n"
            )
            (base.parent / f"{base.name}-metadata.txt").write_text(meta, encoding="utf-8")
        except Exception:
            pass

        logger.error(
            "Stage [%s] FAILED: URL=%s  error=%s  diagnostics=%s",
            stage, page.url, exc, base.name,
        )

    # ── Create button resolver ─────────────────────────────────────

    async def _ensure_create_button(self, page: Page):
        """Find the single visible Create button using role locator.

        If multiple matches exist, enumerate all with index, text,
        aria-label, and visibility, then select the visible one.
        """
        buttons = page.get_by_role("button", name="Create", exact=True)
        count = await buttons.count()

        if count == 0:
            raise RuntimeError("No Create button found via role locator")

        if count == 1:
            logger.info("Create button found: 1 match")
            return buttons.first

        logger.warning("Create button: %d matches — enumerating", count)
        visible_btn = None
        for i in range(count):
            btn = buttons.nth(i)
            text = await btn.text_content() or ""
            aria = await btn.get_attribute("aria-label") or ""
            visible = await btn.is_visible()
            logger.info(
                "  Create match %d: text=%s aria-label=%s visible=%s",
                i, text.strip()[:40], aria, visible,
            )
            if visible and visible_btn is None:
                visible_btn = btn

        if visible_btn:
            return visible_btn

        logger.warning("No visible Create button found, using first match")
        return buttons.first

    # ── stage detection on failure ─────────────────────────────────

    @staticmethod
    def _detect_stage(page: Page) -> str:
        url = page.url.lower()
        if "studio.youtube.com" not in url:
            return "navigation"
        if "/video/" in url and "/edit" in url:
            return "details_form"
        if "upload" in url:
            return "upload_dialog"
        return "dashboard"

    # ── main upload flow ───────────────────────────────────────────

    async def _upload_playwright(
        self, video_path: Path, title: str, description: str
    ) -> bool:
        self._preflight_check(video_path)

        async with async_playwright() as p:
            context = await self._get_persistent_context(p, self.session)
            if not context:
                return False

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                # ══════════════════════════════════════════════════
                # STAGE 1 — Dashboard
                # ══════════════════════════════════════════════════
                from playwright.async_api import TimeoutError as PlaywrightTimeout

                try:
                    await page.goto(
                        "https://studio.youtube.com",
                        timeout=60000,
                        wait_until="networkidle",
                    )
                except PlaywrightTimeout:
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeout:
                    pass
                await self._log_url(page, "dashboard")
                logger.info("Dashboard loaded")
                await self._screenshot(page, "dashboard_before_create.png")

                # ══════════════════════════════════════════════════
                # STAGE 2 — Create button
                # ══════════════════════════════════════════════════
                create_btn = await self._ensure_create_button(page)
                await create_btn.click()
                await page.wait_for_timeout(1500)
                logger.info("Create clicked")
                await self._screenshot(page, "create_dropdown_open.png")

                # ══════════════════════════════════════════════════
                # STAGE 3 — Upload videos menu item
                # ══════════════════════════════════════════════════
                try:
                    upload_btn = page.get_by_text("Upload videos", exact=True)
                    await upload_btn.wait_for(timeout=5000)
                except PlaywrightTimeout:
                    upload_btn = page.locator("[role='menuitem']").filter(
                        has_text="Upload videos"
                    )
                    await upload_btn.wait_for(timeout=5000)

                await upload_btn.click()
                await page.wait_for_timeout(2000)
                logger.info("Upload videos clicked")
                await self._screenshot(page, "upload_videos_clicked.png")

                # ══════════════════════════════════════════════════
                # STAGE 4 — File picker + upload
                # ══════════════════════════════════════════════════
                file_input = page.locator("input[type='file']")
                await file_input.wait_for(state="attached", timeout=20000)
                logger.info("File selected")
                await self._screenshot(page, "upload_dialog_ready.png")
                await file_input.set_input_files(str(video_path))

                # ══════════════════════════════════════════════════
                # STAGE 5 — Title + Description fill
                # ══════════════════════════════════════════════════
                title_input = page.locator(
                    "#title-textarea, #textbox, ytcp-video-title-form #textbox"
                ).first
                await title_input.wait_for(timeout=120000)
                await self._log_url(page, "details")
                await title_input.click()
                await title_input.press_sequentially(title, delay=20)
                logger.info("Title filled")

                desc_input = page.locator(
                    "ytcp-video-description-form #textbox, "
                    "#description-textarea, "
                    "ytcp-social-suggestions-textbox[label*='escription']"
                ).first
                try:
                    await desc_input.click(timeout=5000)
                    await desc_input.press_sequentially(description, delay=10)
                    logger.info("Description filled")
                except Exception:
                    logger.warning("Description input not found, continuing without description")

                await self._screenshot(page, "metadata_filled.png")

                # ══════════════════════════════════════════════════
                # STAGE 6 — Wait for user to complete manually
                # ══════════════════════════════════════════════════
                print()
                print("=" * 60)
                print("  YOUTUBE UPLOAD — READY")
                print("=" * 60)
                print(f"  Title:       {title}")
                print(f"  Description: {description[:100]}{'...' if len(description) > 100 else ''}")
                print(f"  Video:       {video_path}")
                print()
                print("  Please complete the remaining YouTube steps")
                print("  manually and press ENTER when finished.")
                print("=" * 60)
                print()
                input()

                final_url = page.url
                logger.info("Upload complete")
                print(f"  Final YouTube Studio URL: {final_url}")
                return True

            except Exception as e:
                stage = self._detect_stage(page)
                await self._stage_fail(page, stage, e)
                raise
            finally:
                await context.close()


