"""
Open Instagram in Playwright using cookies from .env (see translate_instagram_cookie_into_playwright_version.py).
Optional INSTAGRAM_PROFILE_USERNAME in .env: after load, opens your profile, then opens the Following list.
Uses a persistent Firefox profile and notification permission so site state survives runs; screenshot → test_Instagram/.
"""

from __future__ import annotations

import re
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

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from tempdata import following_usernames  # noqa: E402

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


def _open_following_list(page) -> None:
    """On profile page, click Following (stats row or /username/following/ link) and wait for the modal if shown."""
    try:
        page.locator('a[href*="/following"]').first.click(timeout=12_000)
    except Exception:
        try:
            page.get_by_role("link", name=re.compile(r"following", re.I)).first.click(timeout=12_000)
        except Exception:
            page.locator("span").filter(has_text=re.compile(r"^\s*following\s*$", re.I)).first.click(
                timeout=8_000
            )
    page.wait_for_timeout(1500)
    try:
        page.locator('[role="dialog"]').first.wait_for(state="visible", timeout=10_000)
    except Exception:
        pass

def _open_following_profile(page, username: str) -> None:
    """Click the account row inside the open Following modal (not a global .first link — sidebar can be disabled)."""
    u = username.lstrip("@").strip()
    dialog = page.locator('[role="dialog"]').first
    dialog.wait_for(state="visible", timeout=15_000)
    # Only links inside the modal; page-wide `a[href="..."] .first` hits background UI (disabled under overlay).
    row_link = dialog.locator(f'a[href="/{u}/"]').first
    row_link.scroll_into_view_if_needed(timeout=10_000)
    try:
        row_link.click(timeout=12_000)
    except Exception:
        try:
            row_link.click(timeout=12_000, force=True)
        except Exception:
            row_link.evaluate("el => el.click()")
    page.wait_for_url(lambda url: f"/{u}/" in url, timeout=25_000)
    page.wait_for_timeout(1500)
    _scroll_profile_down(page, times=3)
    _save_graphql_json(page, u)


def _scroll_profile_down(page, times: int) -> None:
    """Scroll the profile page down a few times to load more content."""
    for _ in range(times):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(1000)

def _save_graphql_json(page, username: str) -> None:
    """Save a snapshot of the current page HTML under temp_download."""
    out_dir = PROJECT_ROOT / "temp_download" / username
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"graphql_{username}.json").write_text(page.content(), encoding="utf-8")


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
            for username in following_usernames:
                _open_following_list(page)
                _open_following_profile(page, username)
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=25_000)
                except Exception:
                    page.goto(
                        f"{INSTAGRAM_ORIGIN}/{profile_user.lstrip('@').strip()}/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                page.wait_for_timeout(500)
        page.screenshot(path=str(out_path), full_page=True)
        context.close()

    print(f"Screenshot saved to {out_path}")


if __name__ == "__main__":
    main()
