"""
Open Instagram in Playwright using cookies from .env (see translate_instagram_cookie_into_playwright_version.py).
Optional INSTAGRAM_PROFILE_USERNAME in .env: after load, clicks that profile link (or opens /username/ if no link).
Uses a persistent Firefox profile and notification permission so site state survives runs; screenshot → test_Instagram/.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from translate_instagram_cookie_into_playwright_version import (  # noqa: E402
    PROJECT_ROOT,
    load_instagram_cookies_for_playwright,
    read_instagram_profile_username,
    read_instagram_target_url,
)

SCREENSHOT_DIR = PROJECT_ROOT / "test_Instagram"
# Same folder each run = Firefox remembers cookies, localStorage, and notification permission for the origin.
# grant_permissions() + one in-page click (below) match site settings + Instagram’s own “already answered” state.
PERSISTENT_PROFILE_DIR = PROJECT_ROOT / ".playwright_instagram_profile"
INSTAGRAM_ORIGIN = "https://www.instagram.com"


def _open_profile(page, username: str) -> None:
    """Click the sidebar/header profile link; if it is not found, open the profile URL."""
    u = username.lstrip("@").strip()
    profile = f"{INSTAGRAM_ORIGIN}/{u}/"
    try:
        page.locator(f'a[href="/{u}/"]').first.click(timeout=15_000)
        page.wait_for_url(lambda url: f"/{u}/" in url, timeout=20_000)
    except Exception:
        page.goto(profile, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1500)


def _dismiss_instagram_notification_prompt_if_present(page) -> None:
    """Click Not Now or Turn On when the sheet is visible so Instagram stores it in this profile."""
    choice = page.get_by_role("button", name="Not Now").or_(
        page.get_by_role("button", name="Turn On")
    )
    try:
        choice.first.click(timeout=2_000)
        page.wait_for_timeout(800)
    except Exception:
        pass


def main() -> None:
    cookies = load_instagram_cookies_for_playwright()
    target = read_instagram_target_url()

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    PERSISTENT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCREENSHOT_DIR / "instagram_page.png"

    with sync_playwright() as p:
        context = p.firefox.launch_persistent_context(
            str(PERSISTENT_PROFILE_DIR),
            headless=False,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        context.grant_permissions(["notifications"], origin=INSTAGRAM_ORIGIN)
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        _dismiss_instagram_notification_prompt_if_present(page)
        profile_user = read_instagram_profile_username()
        if profile_user:
            _open_profile(page, profile_user)
        page.screenshot(path=str(out_path), full_page=True)
        context.close()

    print(f"Screenshot saved to {out_path}")


if __name__ == "__main__":
    main()
