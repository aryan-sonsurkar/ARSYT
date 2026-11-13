from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import get_settings
from database.models import Video
from database.repository import Repository
from src.agents.strategy_agent import StrategyAgent
from src.publish_pack_generator import PublishPackGenerator
from src.caption_generator import CaptionGenerator
from src.content_generator import ContentGenerator
from src.image_generator import ImageGenerator
from src.script_generator import ScriptGenerator
from src.system.cleanup import CleanupEngine
from src.system.storage_monitor import StorageMonitor
from src.uploader import YouTubeUploader
from src.video_assembler import VideoAssembler
from src.voice_generator import VoiceGenerator

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    log_file = (
        settings.logs_dir
        / f"codeshorts_{datetime.now().strftime('%Y%m%d')}.log"
    )

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logger.info("Logging initialized. Log file: %s", log_file)


class CodeShortsBot:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()
        self.content_gen = ContentGenerator()
        self.script_gen = ScriptGenerator()
        self.voice_gen = VoiceGenerator()
        self.image_gen = ImageGenerator()
        self.caption_gen = CaptionGenerator()
        self.video_assembler = VideoAssembler()
        self.youtube_uploader = YouTubeUploader()
        self.publish_pack_gen = PublishPackGenerator()
        self.strategy_agent = StrategyAgent()
        self.storage_monitor = StorageMonitor()
        self.cleanup_engine = CleanupEngine()
        self._topic_data: dict = {}
        self._image_paths: list[Path] = []

    def run_once(self) -> Optional[Video]:
        logger.info("=" * 60)
        logger.info("Starting daily CodeShortsBot generation")
        logger.info("=" * 60)

        video = Video()
        try:
            video = self._generate_topic(video)
            video = self._generate_script(video)
            video = self._generate_voice(video)
            video = self._generate_images(video)
            video = self._generate_captions(video)
            video = self._assemble_video(video)
            video = self._generate_publish_pack(video)
            logger.info("Daily generation completed successfully")
        except Exception as e:
            logger.critical(
                "Fatal error in generation pipeline: %s", e, exc_info=True
            )
            video.status = "failed"
            self.db.save_video(video)
            raise

        return video

    def run_weekly_maintenance(self) -> None:
        logger.info("=" * 60)
        logger.info("Starting weekly maintenance")
        logger.info("=" * 60)

        if self.settings.strategy_enabled:
            try:
                self.strategy_agent.run_weekly_review()
            except Exception as e:
                logger.error("Strategy review failed: %s", e)

        self._backup_database()

        if self.settings.cleanup_enabled:
            try:
                result = self.cleanup_engine.run()
                self.cleanup_engine.generate_report(result)
            except Exception as e:
                logger.error("Storage cleanup failed: %s", e)

        logger.info("Weekly maintenance complete")

    def _generate_topic(self, video: Video) -> Video:
        import time
        stage_start = time.monotonic()
        logger.info("--- Step 1/7: Generating topic ---")
        used_topics = {t.topic for t in self.db.get_unused_topics()}

        weights = None
        if self.settings.strategy_enabled:
            try:
                weights = self.strategy_agent.get_weighted_categories()
                logger.info("Using adaptive category weights: %s", weights)
            except Exception as e:
                logger.warning(
                    "Could not load category weights: %s", e
                )

        self._topic_data = self.content_gen.generate_topic_and_content(
            used_topics, weights
        )

        topic_id = self.db.add_topic(
            self._topic_data["topic"], self._topic_data["category"]
        )
        video.topic_id = topic_id
        video.status = "topic_generated"
        self.db.save_video(video)
        stage_dur = time.monotonic() - stage_start
        logger.info(
            "STAGE topic+content generation: topic='%s' category='%s' duration=%.2fs",
            self._topic_data["topic"],
            self._topic_data["category"],
            stage_dur,
        )
        return video

    def _generate_script(self, video: Video) -> Video:
        import time
        stage_start = time.monotonic()
        logger.info("--- Step 2/7: Generating script ---")
        script_data = self.script_gen.generate_script(
            topic=self._topic_data["topic"],
            category=self._topic_data["category"],
            content=self._topic_data["content"],
        )

        video.title = script_data["title"]
        video.script = script_data["script"]
        video.description = script_data["description"]
        video.hashtags = script_data["hashtags"]
        video.status = "script_generated"
        self.db.save_video(video)

        script_path = (
            self.settings.scripts_dir / f"{video.id or 'latest'}_script.txt"
        )
        script_path.write_text(
            f"Title: {video.title}\n\nScript:\n{video.script}\n\n"
            f"Description:\n{video.description}\n\nHashtags:\n{video.hashtags}",
            encoding="utf-8",
        )
        stage_dur = time.monotonic() - stage_start
        logger.info(
            "STAGE script generation: topic='%s' duration=%.2fs",
            video.title, stage_dur,
        )
        logger.info("Script saved: %s", script_path)
        return video

    def _generate_voice(self, video: Video) -> Video:
        logger.info("--- Step 3/7: Generating voice narration ---")
        voice_path = self.voice_gen.generate(
            script=video.script,
            output_path=self.settings.voices_dir
            / f"{video.id or 'latest'}_narration.wav",
        )
        video.voice_path = str(voice_path)
        video.status = "voice_generated"
        self.db.save_video(video)
        logger.info("Voice saved: %s", voice_path)
        return video

    def _generate_images(self, video: Video) -> Video:
        logger.info("--- Step 4/7: Generating images ---")
        image_paths = self.image_gen.generate_images(
            script=video.script,
            topic=video.title,
            content=self._topic_data.get("content", ""),
            video_id=video.id,
        )
        self._image_paths = image_paths
        video.status = "images_generated"
        self.db.save_video(video)
        logger.info("Generated %d images", len(image_paths))
        return video

    def _generate_captions(self, video: Video) -> Video:
        logger.info("--- Step 5/7: Generating captions ---")
        voice_path = Path(video.voice_path) if video.voice_path else None
        if voice_path and voice_path.exists():
            try:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        str(voice_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                data = json.loads(result.stdout)
                duration = float(
                    data.get("format", {}).get("duration", 0)
                )
            except Exception:
                duration = len(video.script.split()) / 2.5
        else:
            duration = len(video.script.split()) / 2.5

        srt_path = self.caption_gen.generate_srt(
            script=video.script,
            total_duration=duration,
            output_path=self.settings.subtitles_dir
            / f"{video.id or 'latest'}_captions.srt",
        )
        video.status = "captions_generated"
        self.db.save_video(video)
        logger.info("Captions saved: %s", srt_path)
        return video

    def _assemble_video(self, video: Video) -> Video:
        logger.info("--- Step 6/7: Assembling video ---")
        image_paths = self._image_paths
        if not image_paths:
            image_paths = sorted(self.settings.images_dir.glob("frame_*.*"))
        if not image_paths:
            raise FileNotFoundError("No images available for assembly")

        voice_path = Path(video.voice_path) if video.voice_path else None
        if not voice_path or not voice_path.exists():
            raise FileNotFoundError(
                f"Voice file not found: {video.voice_path}"
            )

        srt_path = (
            self.settings.subtitles_dir
            / f"{video.id or 'latest'}_captions.srt"
        )

        output_path = self.video_assembler.assemble(
            image_paths=image_paths,
            audio_path=voice_path,
            srt_path=srt_path
            if srt_path.exists()
            else self.settings.subtitles_dir / "captions.srt",
            script=video.script,
            topic=video.title,
            output_path=self.settings.outputs_dir
            / f"{video.id or 'latest'}_final_video.mp4",
        )

        video.video_path = str(output_path)
        video.status = "video_assembled"

        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(result.stdout)
            video.duration_seconds = float(
                data.get("format", {}).get("duration", 0)
            )
        except Exception:
            video.duration_seconds = len(video.script.split()) / 2.5

        self.db.save_video(video)
        logger.info(
            "Video assembled: %s (%.1fs)",
            output_path,
            video.duration_seconds,
        )
        return video

    def _generate_publish_pack(self, video: Video) -> Video:
        logger.info("--- Step 7/7: Generating publish pack ---")
        self.publish_pack_gen.generate(video)
        video.status = "publish_pack_generated"
        self.db.save_video(video)
        logger.info("Publish pack generated for: %s", video.title)
        print(f"\n{'=' * 60}")
        print(f"  PIPELINE COMPLETE")
        print(f"{'=' * 60}")
        print(f"  Video  : {video.video_path}")
        print(f"  Title  : {video.title}")
        print(f"  Pack   : reports/video_{video.id or 'latest'}_publish_pack.txt")
        print(f"")
        print(f"  NEXT STEPS:")
        print(f"  1. Import video into CapCut")
        print(f"  2. Improve subtitles, add sound effects")
        print(f"  3. Upload manually to YouTube Studio")
        print(f"  4. Use the publish pack for title/caption/hashtags")
        print(f"{'=' * 60}\n")
        return video

    def _wait_for_approval(self, video: Video) -> None:
        print(f"\n{'='*60}")
        print(f"  APPROVAL MODE")
        print(f"{'='*60}")
        print(f"  Video ready: {video.video_path}")
        print(f"  Title: {video.title}")
        print(f"  Duration: {video.duration_seconds:.1f}s")
        print(f"  Script length: {len(video.script.split())} words")
        print(f"{'='*60}")
        while True:
            resp = input("  Upload this video? (yes/no/skip): ").strip().lower()
            if resp in ("yes", "y"):
                logger.info("User approved upload: %s", video.title)
                break
            elif resp in ("no", "n"):
                logger.warning("User rejected upload: %s", video.title)
                video.status = "rejected"
                self.db.save_video(video)
                raise SystemExit("Upload rejected by user")
            elif resp in ("skip", "s"):
                logger.info("User skipped upload: %s", video.title)
                video.status = "skipped"
                self.db.save_video(video)
                raise SystemExit("Upload skipped by user")
            print("  Please answer 'yes', 'no', or 'skip'")

    def _upload_video(self, video: Video) -> None:
        import asyncio

        logger.info("--- Step 7/7: Uploading video ---")
        video_path = Path(video.video_path) if video.video_path else None
        if not video_path or not video_path.exists():
            logger.warning("Video file not found, skipping upload")
            return

        logger.info("Uploading to YouTube...")
        logger.info("START upload pipeline (async via asyncio.run)")
        youtube_success = asyncio.run(
            self.youtube_uploader.upload(
                video_path=video_path,
                title=video.title,
                description=video.description,
                hashtags=video.hashtags,
            )
        )
        logger.info("END upload pipeline: result=%s", youtube_success)
        self.db.update_upload_status(
            video.id,
            "youtube",
            youtube_success,
            "" if youtube_success else "YouTube upload failed",
        )
        if youtube_success:
            logger.info("YouTube upload successful")
        else:
            logger.error("YouTube upload failed")

        video.uploaded_youtube = youtube_success
        video.status = "completed" if youtube_success else "upload_failed"
        self.db.save_video(video)

    def _backup_database(self) -> None:
        db_path = self.settings.database_path
        if not db_path.exists():
            return
        backup_dir = self.settings.database_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = (
            backup_dir
            / f"codeshorts_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )
        try:
            shutil.copy2(str(db_path), str(backup_path))
            logger.info("Database backed up: %s", backup_path)
        except Exception as e:
            logger.error("Database backup failed: %s", e)


def daily_job() -> None:
    bot = CodeShortsBot()
    try:
        bot.run_once()
    except Exception as e:
        logger.error("Daily job failed: %s", e)


def weekly_maintenance_job() -> None:
    bot = CodeShortsBot()
    try:
        bot.run_weekly_maintenance()
    except Exception as e:
        logger.error("Weekly maintenance failed: %s", e)


def serve_forever() -> None:
    settings = get_settings()
    scheduler = BackgroundScheduler(daemon=True)

    hour_str, minute_str = settings.schedule_time.split(":")
    daily_trigger = CronTrigger(hour=int(hour_str), minute=int(minute_str))

    scheduler.add_job(
        daily_job,
        trigger=daily_trigger,
        id="daily_codeshorts",
        name="Daily CodeShorts Generation",
        misfire_grace_time=3600,
    )

    sunday_trigger = CronTrigger(
        day_of_week=settings.strategy_review_day,
        hour=int(hour_str) + 1 if int(hour_str) + 1 < 24 else 0,
        minute=int(minute_str),
    )
    scheduler.add_job(
        weekly_maintenance_job,
        trigger=sunday_trigger,
        id="weekly_maintenance",
        name="Weekly Strategy Review + Cleanup",
        misfire_grace_time=7200,
    )

    scheduler.start()
    logger.info(
        "Scheduler started. Daily job at %s, weekly maintenance on Sunday.",
        settings.schedule_time,
    )

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)


def _run_create_profile() -> None:
    """Create the dedicated CodeShortsBot automation profile.

    Launches Chrome with a new profile, lets the user log into YouTube
    Studio, then saves the profile path so future sessions reuse it.
    """
    from src.uploader import _detect_browser, _default_user_data_dir
    import subprocess

    settings = get_settings()

    raw_dir = settings.chrome_automation_profile_dir
    if raw_dir:
        profile_dir = Path(raw_dir)
    else:
        detected = _default_user_data_dir()
        if not detected:
            print("ERROR: Could not find Chrome User Data directory.")
            print("Please set CHROME_AUTOMATION_PROFILE_DIR in your .env file.")
            return
        profile_dir = Path(detected) / "CodeShortsBot"

    profile_dir.mkdir(parents=True, exist_ok=True)

    binfo = _detect_browser()
    chrome_path = binfo["path"]
    if not chrome_path:
        print("ERROR: Chrome executable not found.")
        return

    print(f"\n{'='*60}")
    print(f"  CREATING AUTOMATION PROFILE")
    print(f"{'='*60}")
    print(f"  Profile path : {profile_dir}")
    print(f"")
    print(f"  A Chrome window will open for the dedicated")
    print(f"  CodeShortsBot profile.")
    print(f"")
    print(f"  1. Log into YouTube Studio (studio.youtube.com)")
    print(f"  2. Close the Chrome window when done")
    print(f"")
    print(f"  Then run:  python main.py --verify-youtube")
    print(f"{'='*60}\n")

    subprocess.run(
        [
            chrome_path,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://studio.youtube.com",
        ]
    )

    # After Chrome closes, verify the profile was set up.
    cookie_paths = [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    ]
    ok = any(p.exists() for p in cookie_paths)
    if ok:
        print(f"\n  [PASS] Profile created successfully.")
        print(f"  Cookie database found at one of:")
        for p in cookie_paths:
            if p.exists():
                print(f"    {p}")
        print(f"\n  Run:  python main.py --verify-youtube")
    else:
        print(f"\n  Profile directory was created but no cookie")
        print(f"  database was found.  Make sure you logged into")
        print(f"  YouTube Studio before closing Chrome.")
        print(f"\n  To retry:  python main.py --create-profile")


def _run_list_profiles() -> None:
    """Scan Chrome user data directory and list all detected profiles."""
    from src.uploader import list_chrome_profiles, _default_user_data_dir
    from config.settings import get_settings

    settings = get_settings()
    raw_dir = settings.chrome_user_data_dir
    if raw_dir:
        root = Path(raw_dir)
    else:
        detected = _default_user_data_dir()
        if detected:
            root = Path(detected)
        else:
            print("Chrome user data directory not found.")
            print("Set CHROME_USER_DATA_DIR in .env or install Chrome.")
            return

    print(f"\nChrome User Data Dir: {root}")
    print(f"Configured profile   : {settings.chrome_profile_name}")
    print()

    profiles = list_chrome_profiles()
    if not profiles:
        print("No Chrome profiles detected.")
        return

    print(f"Detected profiles ({len(profiles)}):")
    print(f"  {'Name':<15} {'Display':<25} Path")
    print(f"  {'─'*15} {'─'*25} {'─'*40}")
    for p in profiles:
        marker = " ← current" if p["name"] == settings.chrome_profile_name else ""
        print(
            f"  {p['name']:<15} {p['display']:<25} {p['path']}{marker}"
        )
    print()


async def _verify_core(
    label: str, uploader_cls: type, extract_details=None
) -> None:
    """Shared logic for --verify-youtube.

    Launches the real Chrome profile, navigates to the platform,
    checks auth state inline (URL + element selector), and optionally
    runs *extract_details* on the page after a successful validation.
    """
    from playwright.async_api import (
        async_playwright,
        TimeoutError as PlaywrightTimeout,
    )

    print(f"\n{'='*60}")
    print(f"  VERIFYING: {label}")
    print(f"{'='*60}")
    try:
        uploader = uploader_cls()
        session = uploader.session

        if not session.is_ready:
            print(
                f"  Profile : {session.profile_dir}"
            )
            print(
                f"  RESULT:  [FAIL] Profile not found.\n"
                f"  Make sure Chrome is installed and you have "
                f"used it at least once."
            )
            return

        print(f"  Profile path : {session.profile_dir}")
        print(f"  Browser exe  : {session._browser_info.get('path') or '(bundled)'}")
        print(f"  Opening      : {uploader._login_url}")
        print(f"  Please wait... (a browser window will open briefly)\n")

        async with async_playwright() as p:
            context = await session.get_context(p)

            # Log all pages that Chrome opened at startup.
            print(f"  Context pages: {len(context.pages)}")
            for idx, pg in enumerate(context.pages):
                print(f"    Page {idx}: {pg.url}")

            # Use the FIRST existing page (the one the user sees).
            # Do NOT create a new page — that opens a hidden background tab.
            page = context.pages[0] if context.pages else await context.new_page()

            before_url = page.url
            target_url = uploader._login_url
            print(f"  Before goto  : {before_url}")
            print(f"  Target       : {target_url}")

            try:
                try:
                    await page.goto(
                        target_url,
                        timeout=60000,
                        wait_until="networkidle",
                    )
                except PlaywrightTimeout:
                    # YouTube Studio is a heavy SPA that may never reach
                    # networkidle (polling, WebSockets).  Catch the
                    # timeout gracefully — the navigation itself may
                    # have succeeded.
                    pass

                after_url = page.url
                print(f"  After goto   : {after_url}")

                # Raise explicit error if page never left about:blank.
                if after_url in ("about:blank",):
                    raise RuntimeError(
                        f"Navigation to {target_url} failed — "
                        f"page is still {after_url!r}"
                    )

                # 1) URL check — page should not have been redirected to login.
                expected = target_url.rstrip("/")
                url_ok = after_url.startswith(expected)

                # 2) Element check — logged-in indicator visible.
                el_ok = False
                if url_ok:
                    try:
                        await page.wait_for_selector(
                            uploader._success_indicator, timeout=5000
                        )
                        el_ok = True
                    except Exception as e:
                        logger.debug("Auth element check failed: %s", e)

                if url_ok and el_ok:
                    print(f"  Login status : LOGGED IN")
                    details = ""
                    if extract_details:
                        try:
                            details = await extract_details(page)
                        except Exception as e:
                            details = f"(could not read: {e})"
                    if details:
                        print(f"  Channel      : {details}")
                    print(f"  RESULT:  [PASS] Session is valid.\n")
                else:
                    print(f"  Login status : NOT LOGGED IN")
                    if not url_ok:
                        print(f"  Reason       : URL redirected to '{after_url}' "
                              f"(expected '{expected}')")
                    if not el_ok:
                        print(f"  Reason       : Auth element "
                              f"'{uploader._success_indicator}' not found")
                    print(
                        f"  RESULT:  [FAIL] Not logged in.\n"
                        f"  Please log into {uploader._login_url} in your\n"
                        f"  normal Chrome, close ALL Chrome windows, and\n"
                        f"  run this command again."
                    )
            finally:
                await context.close()

    except Exception as e:
        print(f"  ERROR:   {e}\n")


async def _extract_youtube_channel(page) -> str:
    """Try to read the YouTube Studio channel display name from the page."""
    for sel in (
        "#channel-name",
        "ytcp-account-photo",
        "#account-name",
        "span#account-name",
        "ytcp-avatar",
        "header .profile-name",
        "header ytcp-avatar",
    ):
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                text = await el.inner_text(timeout=1000)
                text = text.strip()
                if text:
                    return text
        except Exception:
            continue
    # Fallback: page title.
    title = await page.title()
    # YouTube Studio titles often contain the channel name.
    if " - YouTube Studio" in title:
        return title.replace(" - YouTube Studio", "").strip()
    return "(not found)"


def _run_verify_youtube() -> None:
    import asyncio
    from src.uploader import YouTubeUploader

    asyncio.run(_verify_core("YouTube", YouTubeUploader, _extract_youtube_channel))


def _run_diagnostics() -> None:
    """Check all system components and report PASS/FAIL for each."""
    import shutil
    import subprocess as _subprocess

    from config.settings import get_settings as _get_settings

    checks: list[tuple[str, str, bool, str]] = []

    # Python
    v = sys.version_info
    checks.append(("Python", f"{v.major}.{v.minor}.{v.micro}", True, ""))

    # FFmpeg
    try:
        r = _subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=10,
        )
        ok = r.returncode == 0
        ver = r.stdout.split("\n")[0] if ok else ""
        checks.append(("FFmpeg", ver if ok else "(not found)", ok,
                        "Install ffmpeg from https://ffmpeg.org" if not ok else ""))
    except Exception:
        checks.append(("FFmpeg", "(not found)", False,
                       "Install ffmpeg from https://ffmpeg.org"))

    # Ollama
    try:
        r = _subprocess.run(
            ["ollama", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        ok = r.returncode == 0
        ver = r.stdout.strip() if ok else ""
        checks.append(("Ollama", ver, ok,
                       "Install Ollama from https://ollama.com" if not ok else ""))
    except Exception:
        checks.append(("Ollama", "(not found)", False,
                       "Install Ollama from https://ollama.com"))

    # Chrome
    from src.uploader import _detect_browser, _default_user_data_dir
    binfo = _detect_browser()
    chrome_ok = binfo["path"] is not None
    checks.append(("Chrome (executable)",
                   binfo["path"] or "(not found)", chrome_ok,
                   "Install Google Chrome" if not chrome_ok else ""))

    # Chrome automation profile
    settings = _get_settings()
    auto_dir = settings.chrome_automation_profile_dir
    detected_user_data = _default_user_data_dir()
    if auto_dir:
        user_data_dir = Path(auto_dir)
    elif detected_user_data:
        user_data_dir = Path(detected_user_data) / "CodeShortsBot"
    else:
        user_data_dir = None

    profile_dir = user_data_dir / "Default" if user_data_dir else None

    # Collect details for diagnostic output
    print(f"  {'':>9}  {'CHROME_AUTOMATION_PROFILE_DIR':<30} {repr(auto_dir)}")
    if not auto_dir:
        print(f"  {'':>9}  {'resolved (default)':<30} {user_data_dir or '(none)'}")

    user_data_ok = user_data_dir is not None and user_data_dir.exists()
    profile_dir_ok = profile_dir is not None and profile_dir.exists()

    # Chrome stores cookies in one of two locations depending on version.
    cookie_paths = [
        profile_dir / "Cookies",
        profile_dir / "Network" / "Cookies",
    ] if profile_dir else []
    found_cookie = None
    for p in cookie_paths:
        if p.exists():
            found_cookie = p
            break
    cookies_ok = found_cookie is not None
    profile_ok = profile_dir_ok and cookies_ok

    print(f"  {'':>9}  {'automation user_data_dir':<30} {user_data_dir or '(none)'}")
    print(f"  {'':>9}  {'profile dir (Default/)':<30} {profile_dir or '(none)'}")
    print(f"  {'':>9}  {'user_data_dir exists':<30} {user_data_ok}")
    print(f"  {'':>9}  {'profile dir exists':<30} {profile_dir_ok}")
    print(f"  {'':>9}  {'Cookie DB found':<30} {found_cookie or 'no'}")

    reasons = []
    if user_data_dir is None:
        reasons.append("CHROME_AUTOMATION_PROFILE_DIR not set and auto-detection failed")
    else:
        if not user_data_ok:
            reasons.append(f"Automation dir does not exist: {user_data_dir}")
        if not profile_dir_ok:
            reasons.append(f"Profile dir does not exist: {profile_dir}")
        if not cookies_ok:
            reasons.append(
                f"No cookie database found — checked {cookie_paths[0]} "
                f"and {cookie_paths[1]}"
            )

    hint = "; ".join(reasons) if reasons else ""
    checks.append(("Chrome profile",
                   str(profile_dir) if profile_ok else hint,
                   profile_ok,
                   "See details above" if not profile_ok else ""))

    # Visual Assets (ComfyUI / Flux no longer required — using internet images)

    # GPU
    import platform as _platform
    if _platform.system() == "Windows":
        try:
            import ctypes
            nvml = ctypes.windll.LoadLibrary("nvml.dll")
            nvml.nvmlInit()
            handle = ctypes.c_void_p()
            nvml.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle))
            name_buf = ctypes.create_string_buffer(256)
            nvml.nvmlDeviceGetName(handle, name_buf, 256)
            mem_info = (ctypes.c_uint64 * 3)()
            nvml.nvmlDeviceGetMemoryInfo(handle, mem_info)
            free_gb = mem_info[0] / (1024 ** 3)
            nvml.nvmlShutdown()
            gpu_ok = free_gb > 4
            checks.append((f"GPU ({name_buf.value.decode()})",
                           f"{free_gb:.1f}GB free", gpu_ok, ""))
        except Exception as e:
            checks.append(("GPU", f"(detection failed: {e})", False, ""))
    else:
        try:
            r = _subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                parts = r.stdout.strip().split(",")
                gpu_ok = float(parts[1].strip()) > 4096
                checks.append((f"GPU ({parts[0].strip()})",
                               f"{parts[1].strip()}MB free", gpu_ok, ""))
            else:
                checks.append(("GPU", "(none detected)", True, ""))
        except Exception:
            checks.append(("GPU", "(none detected)", True, ""))

    # Disk space
    try:
        disk = shutil.disk_usage(settings.project_root)
        free_gb = disk.free / (1024 ** 3)
        disk_ok = free_gb > 1
        checks.append(("Disk space", f"{free_gb:.1f}GB free", disk_ok, ""))
    except Exception:
        checks.append(("Disk space", "(unknown)", True, ""))

    # Print report
    print(f"\n{'='*60}")
    print(f"  SYSTEM DIAGNOSTICS")
    print(f"{'='*60}")
    all_pass = True
    for name, detail, ok, hint in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status:>4}]  {name:<25} {detail}")
        if not ok and hint:
            print(f" {'':>7}  {'':<25} {hint}")
        if not ok:
            all_pass = False
    print(f"{'='*60}")
    print(f"  OVERALL: {'All checks passed' if all_pass else 'Some checks failed'}")
    print(f"{'='*60}\n")


def _get_latest_video() -> Optional[Path]:
    videos = sorted(
        get_settings().outputs_dir.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for v in videos:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type:format=duration",
                 "-of", "json", str(v)],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(r.stdout)
            has_video = any(s.get("codec_type") == "video" for s in data.get("streams", []))
            dur = float(data.get("format", {}).get("duration", 0))
            if has_video and dur > 0:
                return v
        except Exception:
            continue
    return None


def _get_latest_audio() -> Optional[Path]:
    files = sorted(
        get_settings().voices_dir.glob("*_narration.wav"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return files[0] if files else None


def _get_latest_caption_file() -> Optional[Path]:
    files = sorted(
        get_settings().subtitles_dir.glob("*_captions.srt"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return files[0] if files else None


def _run_test_render() -> None:
    settings = get_settings()

    # Use topic_11 images
    src_dir = settings.project_root / "assets" / "topic_11"
    if not src_dir.exists():
        print("FAIL: assets/topic_11 not found")
        return
    valid_ext = {".jpg", ".jpeg", ".png", ".webp"}
    images = sorted(
        [p for p in src_dir.iterdir() if p.suffix.lower() in valid_ext and p.is_file()],
        key=lambda p: p.name.lower(),
    )
    if not images:
        print("FAIL: No images found in assets/topic_11")
        return
    print(f"Using images: {len(images)} from {src_dir}")

    # Latest audio
    audio = _get_latest_audio()
    if not audio or not audio.exists():
        print("FAIL: No narration audio found in voices/")
        return
    print(f"Using audio: {audio}")

    # Latest captions
    captions = _get_latest_caption_file()
    if not captions or not captions.exists():
        print("FAIL: No caption file found in subtitles/")
        return
    print(f"Using captions: {captions}")

    # Temp script text
    script_text = "Test render script for video assembly validation."

    assembler = VideoAssembler()
    try:
        output = assembler.assemble(
            image_paths=images,
            audio_path=audio,
            srt_path=captions,
            script=script_text,
            topic="Test Render",
            output_path=settings.outputs_dir / "test_render.mp4",
        )
        print(f"\nTEST RENDER SUCCESS")
        print(f"Output: {output}")
    except Exception as e:
        print(f"\nTEST RENDER FAILED: {e}")


def _run_test_publish_pack() -> None:
    from src.publish_pack_generator import PublishPackGenerator
    from database.models import Video

    video = Video()
    video.title = "Why Most Coding Projects Fail After 1,000 Lines"
    video.description = "Learn why large codebases become unmaintainable and how to avoid it."
    video.script = (
        "Most coding projects become impossible to maintain after 1,000 lines. "
        "It's not because the code gets harder to write. It's because the mental model "
        "gets harder to hold. Every function, every variable, every edge case adds weight "
        "to your working memory. Senior developers don't write more code. They write less. "
        "They refactor aggressively, delete dead code, and split modules before they grow "
        "too large. The best code is the code you don't write."
    )
    video.video_path = str(get_settings().outputs_dir / "test_render.mp4")

    print(f"\n{'='*60}")
    print(f"  TEST PUBLISH PACK")
    print(f"{'='*60}")
    gen = PublishPackGenerator()
    pack = gen.generate(video)
    print(f"\n  [PASS] Publish pack generated")
    print(f"  Best title: {pack['best_title']}")
    print(f"  Captions  : {len(pack['all_captions'])}")
    print(f"  Hashtag sets: {len(pack['hashtag_sets'])}")
    print(f"{'='*60}\n")


def _run_test_voice() -> None:
    from src.voice_generator import VoiceGenerator

    sample_script = (
        "Kokoro TTS integration is now complete. "
        "CodeShortsBot generates high quality voice overs "
        "using the Kokoro text to speech model."
    )

    output = get_settings().project_root / "test_voice_kokoro.wav"

    print(f"\n{'='*60}")
    print(f"  TEST VOICE (Kokoro TTS)")
    print(f"{'='*60}")
    print(f"  Generating narration for:")
    print(f"    {sample_script}")
    print(f"  Output: {output}\n")

    vg = VoiceGenerator()
    try:
        result = vg.generate(script=sample_script, output_path=output)
        print(f"\n  [PASS] Voice generated successfully")
        print(f"  Output: {result}")
        print(f"  Size  : {result.stat().st_size / 1024:.1f} KB")

        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "json", str(result)],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(r.stdout)
            dur = float(data.get("format", {}).get("duration", 0))
            print(f"  Duration: {dur:.2f}s")
        except Exception:
            pass
    except Exception as e:
        print(f"\n  [FAIL] {e}")
    print(f"{'='*60}\n")


def _cleanup_workspace() -> None:
    """Clean generated asset directories between video batches."""
    settings = get_settings()
    dirs = [
        ("assets", settings.project_root / "assets"),
        ("outputs", settings.outputs_dir),
        ("voices", settings.voices_dir),
        ("scripts", settings.scripts_dir),
        ("subtitles", settings.subtitles_dir),
        ("image_prompts", settings.project_root / "image_prompts"),
        ("reports", settings.reports_dir),
    ]

    print(f"\n{'=' * 60}")
    print(f"  CLEANUP REPORT")
    print(f"{'=' * 60}")

    total = 0
    for name, path in dirs:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            print(f"  {name+':':<25} 0 files removed (created)")
            continue

        count = 0
        try:
            for item in path.iterdir():
                if item.is_file():
                    item.unlink()
                    count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    count += 1
            total += count
        except Exception as e:
            raise RuntimeError(f"Failed to clean {name} ({path}): {e}")

        print(f"  {name+':':<25} {count} files removed")

    print(f"  {'Total files removed:':<25} {total}")
    if total == 0:
        print(f"  (workspace was already clean)")
    print(f"  Cleanup completed successfully.")
    print(f"{'=' * 60}\n")


def _run_test_upload() -> None:
    import asyncio

    video = _get_latest_video()
    if not video or not video.exists():
        print("FAIL: No video found in outputs/")
        return

    # Quick ffprobe check
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(data.get("format", {}).get("duration", 0))
    except Exception as e:
        print(f"FAIL: Cannot probe video: {e}")
        return

    print(f"Using existing video: {video}")
    print(f"  Duration: {duration:.1f}s  Video: {has_video}  Audio: {has_audio}")

    if not has_video:
        print("FAIL: Video has no video stream")
        return

    uploader = YouTubeUploader()
    print("Launching uploader...")
    success = asyncio.run(
        uploader.upload(
            video_path=video,
            title=f"Test Upload {datetime.now().strftime('%H%M%S')}",
            description="Test upload from --test-upload mode.",
            hashtags="#test",
        )
    )
    if success:
        print("\nTEST UPLOAD SUCCESS")
    else:
        print("\nTEST UPLOAD FAILED")


def main() -> None:
    setup_logging()
    logger.info("CodeShortsBot v2.0.0 starting...")
    logger.info("Project root: %s", get_settings().project_root)

    if "--once" in sys.argv:
        logger.info("Running in one-shot mode")
        daily_job()
    elif "--weekly" in sys.argv:
        logger.info("Running weekly maintenance only")
        weekly_maintenance_job()
    elif "--strategy-report" in sys.argv:
        logger.info("Running strategy report only")
        bot = CodeShortsBot()
        bot.strategy_agent.run_weekly_review()
    elif "--setup-sessions" in sys.argv:
        print(f"\n{'='*60}")
        print(f"  SETUP INSTRUCTIONS")
        print(f"{'='*60}")
        print(f"  CodeShortsBot now uses a dedicated Chrome automation")
        print(f"  profile instead of your Default profile.")
        print(f"")
        print(f"  Step 1: Run:  python main.py --create-profile")
        print(f"  Step 2: Log into YouTube Studio in the opened window.")
        print(f"  Step 3: Close Chrome.")
        print(f"  Step 4: Run:  python main.py --verify-youtube")
        print(f"{'='*60}\n")
    elif "--create-profile" in sys.argv:
        _run_create_profile()
    elif "--list-profiles" in sys.argv:
        _run_list_profiles()
    elif "--diagnostics" in sys.argv:
        _run_diagnostics()
    elif "--verify-session" in sys.argv:
        _run_verify_youtube()
    elif "--verify-youtube" in sys.argv:
        _run_verify_youtube()
    elif "--cleanup" in sys.argv:
        logger.info("Running workspace cleanup")
        _cleanup_workspace()
    elif "--test-render" in sys.argv or "--test-step6" in sys.argv:
        logger.info("Running test render mode")
        _run_test_render()
    elif "--test-publish-pack" in sys.argv:
        logger.info("Running test publish pack mode")
        _run_test_publish_pack()
    elif "--test-voice" in sys.argv:
        logger.info("Running test voice mode")
        _run_test_voice()
    elif "--test-upload" in sys.argv or "--test-step7" in sys.argv:
        logger.info("Running test upload mode")
        _run_test_upload()
    else:
        logger.info("Running in scheduled mode")
        serve_forever()


if __name__ == "__main__":
    main()
