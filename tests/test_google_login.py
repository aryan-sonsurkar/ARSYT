"""Standalone test for Google login via Playwright.

This script:
  1. Launches a persistent browser profile using the same
     anti-detection configuration as the main uploader.
  2. Navigates to accounts.google.com.
  3. Prints browser fingerprint details (navigator.webdriver,
     chrome.runtime, user agent, etc.).
  4. Prints the final URL after any redirects.
  5. Saves a screenshot and page HTML to ``logs/screenshots/``.

Run with::

    python -m tests.test_google_login

Or from the project root::

    python tests/test_google_login.py
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure the project root is on sys.path so that ``from config`` etc.
# work when running the script directly.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from playwright.sync_api import sync_playwright

# Reuse the same anti-detection constants from the uploader.
from src.uploader import (
    _STEALTH_INIT_SCRIPT,
    _STEALTH_ARGS,
    _detect_browser,
)

logger = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────


def _fingerprint(page: Any) -> dict:
    """Collect browser fingerprint details."""
    return page.evaluate(
        """() => ({
            navigator_webdriver: navigator.webdriver,
            navigator_userAgent: navigator.userAgent,
            navigator_platform: navigator.platform,
            navigator_languages: navigator.languages,
            navigator_plugins_length: navigator.plugins.length,
            chrome_runtime: typeof chrome !== 'undefined' && chrome.runtime ? 'present' : 'absent',
            window_chrome: typeof chrome !== 'undefined' ? 'present' : 'absent',
        })"""
    )


def _launch_args_used(page: Any) -> list[str]:
    """Dump Chrome command-line flags visible to JS."""
    try:
        return page.evaluate(
            """() => {
                const div = document.createElement('div');
                div.setAttribute('style', 'display:none');
                document.body.appendChild(div);
                return [];
            }"""
        )
    except Exception:
        return []


# ── main ─────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )

    browser_info = _detect_browser()
    print(f"\n{'='*60}")
    print("  GOOGLE LOGIN DIAGNOSTIC TEST")
    print(f"{'='*60}")
    print(f"  Browser detected : {browser_info['browser_type']}")
    print(f"  Channel           : {browser_info['channel']}")
    print(f"  Executable path   : {browser_info['path'] or '(bundled)'}")
    print(f"  Stealth args      : {_STEALTH_ARGS}")
    print(f"{'='*60}\n")

    screenshots_dir = _project_root / "logs" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")

    with sync_playwright() as p:
        kwargs: dict = {
            "user_data_dir": str(
                _project_root
                / "browser_profiles"
                / "test_google_login"
            ),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "no_viewport": False,
            "args": _STEALTH_ARGS,
        }

        if browser_info["path"]:
            kwargs["channel"] = browser_info["channel"]
            kwargs["executable_path"] = browser_info["path"]

        context = p.chromium.launch_persistent_context(**kwargs)
        context.add_init_script(_STEALTH_INIT_SCRIPT)

        page = context.new_page()

        # Collect console messages for debugging.
        console_logs: list[str] = []
        page.on("console", lambda msg: console_logs.append(msg.text))

        try:
            print("  Navigating to https://accounts.google.com ...")
            page.goto(
                "https://accounts.google.com",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            time.sleep(3)

            # ── fingerprint ──────────────────────────────────────
            fp = _fingerprint(page)
            print(f"\n{'─'*60}")
            print("  BROWSER FINGERPRINT")
            print(f"{'─'*60}")
            for key, val in fp.items():
                print(f"  {key:40s}: {val}")
            print(f"{'─'*60}\n")

            # ── URL ──────────────────────────────────────────────
            final_url = page.url
            print(f"  Final URL  : {final_url}")

            # Detect login page vs. post-login.
            if "accounts.google.com" in final_url:
                print(
                    "  Login state: LOGIN PAGE (not authenticated yet)"
                )
            elif "myaccount.google.com" in final_url:
                print(
                    "  Login state: ALREADY LOGGED IN (myaccount)"
                )
            elif "google.com" in final_url:
                print(
                    "  Login state: LOGGED IN (other google page)"
                )
            else:
                print(
                    "  Login state: UNKNOWN (redirected away from Google)"
                )

            # ── screenshot ───────────────────────────────────────
            screenshot_path = (
                screenshots_dir / f"{ts}-google-login-test.png"
            )
            page.screenshot(path=str(screenshot_path))
            print(f"  Screenshot  : {screenshot_path}")

            # ── HTML ─────────────────────────────────────────────
            html_path = screenshots_dir / f"{ts}-google-login-test.html"
            try:
                html = page.content()
                html_path.write_text(html, encoding="utf-8")
                print(f"  HTML saved  : {html_path}")
            except Exception as e:
                print(f"  HTML failed : {e}")

            # ── console logs ─────────────────────────────────────
            if console_logs:
                log_path = (
                    screenshots_dir
                    / f"{ts}-google-login-test-console.txt"
                )
                log_path.write_text(
                    "\n".join(console_logs), encoding="utf-8"
                )
                print(f"  Console logs: {log_path}")
            else:
                print("  Console logs: (none captured)")

            # ── fingerprint JSON ─────────────────────────────────
            fp_path = (
                screenshots_dir / f"{ts}-google-login-fingerprint.json"
            )
            fp_path.write_text(
                json.dumps(
                    {
                        "fingerprint": fp,
                        "final_url": final_url,
                        "stealth_args": _STEALTH_ARGS,
                        "browser_info": browser_info,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"  Fingerprint : {fp_path}")

            print(f"\n{'='*60}")
            print("  Test complete. The browser window is still open.")
            print("  You may now log in manually to test auth flow.")
            print("  Close the browser window or press Ctrl+C to exit.")
            print(f"{'='*60}\n")

            # Keep the browser open so the user can log in.
            while True:
                time.sleep(10)
                if not page.is_closed():
                    continue
                break

        except KeyboardInterrupt:
            print("\n  Test interrupted by user.")
        except Exception as e:
            print(f"\n  ERROR: {e}")
            try:
                page.screenshot(
                    path=str(
                        screenshots_dir / f"{ts}-google-login-error.png"
                    )
                )
            except Exception:
                pass
            raise
        finally:
            context.close()


if __name__ == "__main__":
    main()
