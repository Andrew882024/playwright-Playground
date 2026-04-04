"""
Open Instagram in Playwright using cookies from .env (see translate_instagram_cookie_into_playwright_version.py).
Screenshot is written to test_Instagram/ at the project root.
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
    read_instagram_target_url,
)

SCREENSHOT_DIR = PROJECT_ROOT / "test_Instagram"


def main() -> None:
    cookies = load_instagram_cookies_for_playwright()
    target = read_instagram_target_url()

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCREENSHOT_DIR / "instagram_page.png"

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out_path), full_page=True)
        context.close()
        browser.close()

    print(f"Screenshot saved to {out_path}")


if __name__ == "__main__":
    main()
