# Auth Debug Report (v2 — Real Profile Reuse)

## Approach

Stop trying to authenticate via Playwright.  Use the user's **real Chrome profile**
that Google already trusts.

## How it works

1. User logs into YouTube Studio and Instagram manually in their normal Chrome
   (one-time setup).
2. User closes all Chrome windows.
3. User runs ``python main.py --verify-session`` to confirm.
4. CodeShortsBot launches Chrome via Playwright pointing at that **same real
   profile** → inherits all existing cookies and sessions.
5. No automated login is ever attempted.

## Configuration

| `.env` variable | Default | Purpose |
|---|---|---|
| `CHROME_USER_DATA_DIR` | auto-detected | Path to Chrome's User Data folder |
| `CHROME_PROFILE_NAME` | `Default` | Which profile inside that folder |

On Windows the default location is::

    %LOCALAPPDATA%\Google\Chrome\User Data

## Changes from v1

| Area | Before (v1) | After (v2) |
|---|---|---|
| Profile source | Playwright-created empty profile | User's real Chrome profile |
| Login flow | ``first_time_login()`` opens browser, user types credentials, Playwright waits | **Removed entirely** — user logs in manually beforehand |
| Session readiness | Marker file ``.session_ready`` in `browser_profiles/` | Existence of `Cookies` file in the real profile directory |
| `--setup-sessions` | Opened a browser and prompted for login | Prints plain-text instructions only |
| `--verify-session` | *(did not exist)* | Opens YouTube / Instagram in real profile and reports PASS/FAIL |
| CLI usage before upload | *(not required)* | Run ``--verify-session`` once after manual login |

## Manual setup steps

```
1. Log into YouTube Studio   (studio.youtube.com)  in your normal Chrome.
2. Log into Instagram         (instagram.com)       in your normal Chrome.
3. Close ALL Chrome windows.
4. Run:  python main.py --verify-session
   → Both platforms should show PASS.
5. Done.  The bot will now reuse those sessions.
```

## Why this works

Google's "This browser or app may not be secure" error is triggered because
Playwright-created profiles have no history, no cookies, and exhibit
automation fingerprints.  By reusing a real Chrome profile:

- **Cookies** carry a valid Google OAuth / GAIA session.
- **Profile age** — the profile has browsing history, extensions, and
  other signals that make it look like a real user.
- **No login page is ever hit** — the bot navigates straight to the
  authenticated dashboard.
- The stealth init script and launch args still mask the Playwright CDP
  connection at the JS level.

## Known caveats

- **Chrome must be closed** before running the bot.  Chrome locks its
  profile directory; Playwright cannot open it while Chrome is running.
- The `Default` profile is used by default.  If you use a different
  Chrome profile (e.g. "Profile 1"), set `CHROME_PROFILE_NAME` in `.env`.
- If session validation fails, it means cookies expired or the user
  logged out.  Repeating steps 1–3 above is the fix.

## Commands reference

```
python main.py --setup-sessions     # prints instructions
python main.py --verify-session     # opens browser → checks auth → PASS/FAIL
python main.py --once               # full pipeline (topic → upload)
```
