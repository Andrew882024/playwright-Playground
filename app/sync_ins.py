"""
Open Instagram in Playwright using cookies from .env (see translate_instagram_cookie_into_playwright_version.py).

Visits each account in tempdata.py via the Following modal, scrolls the profile, and records:

- Raw GraphQL bodies: temp_download_raw/<user>/posts_graphql_*.json
- Clean scrape bundle: temp_download/<user>/posts.json (title, caption as comment, author, links)
  plus openable_media_urls.json (all CDN URLs from those responses).

Uses a persistent Firefox profile; screenshot → test_Instagram/.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Response, sync_playwright

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
TEMP_DOWNLOAD_RAW = PROJECT_ROOT / "temp_download_raw"
TEMP_DOWNLOAD = PROJECT_ROOT / "temp_download"

# Substrings that suggest this GraphQL JSON carries posts / media / comments (not inbox tray, etc.).
_POST_GRAPHQL_MARKERS = (
    '"shortcode"',
    '"media":{"pk"',
    '"media":{"id"',
    "edge_owner_to_timeline_media",
    "edge_media_to_comment",
    "xdt_shortcode_media",
    "carousel_media",
    "display_url",
    "video_versions",
    "xdt_api__v1__media",
    "xdt_api__v2__media",
    "profile_grid",
    "timeline_connection",
    "preview_comments",
    "edge_media_preview",
)

# JSON keys whose string values are usually real image/video URLs once parsed (fixes \\u0026 vs &).
_MEDIA_URL_KEYS = frozenset(
    {
        "display_url",
        "video_url",
        "thumbnail_src",
        "profile_pic_url",
        "url",
    }
)


def _graphql_body_looks_like_posts(body: str) -> bool:
    return any(m in body for m in _POST_GRAPHQL_MARKERS)


def _extract_timeline_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull media `node` dicts from any `data.*.edges[].node` that has a shortcode (`code`)."""
    out: list[dict[str, Any]] = []
    root = data.get("data")
    if not isinstance(root, dict):
        return out
    for v in root.values():
        if not isinstance(v, dict):
            continue
        edges = v.get("edges")
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if isinstance(node, dict) and node.get("code"):
                out.append(node)
    return out


def _post_title(node: dict[str, Any]) -> str | None:
    h = node.get("headline")
    if isinstance(h, str) and h.strip():
        return h.strip()
    a = node.get("accessibility_caption")
    if isinstance(a, str) and a.strip():
        return a.strip()
    cap = node.get("caption")
    if isinstance(cap, dict):
        text = cap.get("text")
        if isinstance(text, str) and text.strip():
            first = text.strip().split("\n", maxsplit=1)[0].strip()
            return first or None
    return None


def _post_caption_text(node: dict[str, Any]) -> str | None:
    cap = node.get("caption")
    if isinstance(cap, dict):
        t = cap.get("text")
        if isinstance(t, str):
            return t or None
    return None


def _from_username(node: dict[str, Any]) -> str | None:
    u = node.get("user")
    if isinstance(u, dict):
        name = u.get("username")
        if isinstance(name, str) and name:
            return name
    return None


def _openable_urls_for_subtree(obj: object) -> list[str]:
    found: set[str] = set()
    _collect_cdn_urls(obj, found)
    return sorted(found)


def _merge_structured_posts(by_shortcode: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    code = node.get("code")
    if not isinstance(code, str) or not code:
        return
    urls = set(_openable_urls_for_subtree(node))
    rec = {
        "shortcode": code,
        "permalink": f"{INSTAGRAM_ORIGIN}/p/{code}/",
        "from_username": _from_username(node),
        "title": _post_title(node),
        "comment": _post_caption_text(node),
        "openable_media_urls": sorted(urls),
        "taken_at": node.get("taken_at"),
        "media_pk": str(node["pk"]) if node.get("pk") is not None else None,
    }
    if code not in by_shortcode:
        by_shortcode[code] = rec
        return
    prev = by_shortcode[code]
    merged = set(prev["openable_media_urls"]) | urls
    prev["openable_media_urls"] = sorted(merged)
    if prev.get("comment") is None and rec.get("comment") is not None:
        prev["comment"] = rec["comment"]
    if prev.get("title") is None and rec.get("title") is not None:
        prev["title"] = rec["title"]
    if prev.get("from_username") is None and rec.get("from_username") is not None:
        prev["from_username"] = rec["from_username"]


def structured_bundle_from_raw_dir(raw_dir: Path, scraped_username: str) -> dict[str, Any]:
    """Build posts.json payload from all posts_graphql_*.json under raw_dir."""
    by_shortcode: dict[str, dict[str, Any]] = {}
    for path in sorted(raw_dir.glob("posts_graphql_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for node in _extract_timeline_nodes(data):
            _merge_structured_posts(by_shortcode, node)
    posts = sorted(
        by_shortcode.values(),
        key=lambda p: p.get("taken_at") or 0,
        reverse=True,
    )
    return {
        "scraped_profile": scraped_username,
        "posts": posts,
    }


def _collect_cdn_urls(obj: object, out: set[str]) -> None:
    """Gather https URLs that look like Instagram/Facebook CDN media (openable in a browser)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("https://") and (
                k in _MEDIA_URL_KEYS
                or (k.endswith("_url") and ("instagram" in v or "fbcdn" in v))
            ):
                if "cdninstagram" in v or "fbcdn.net" in v or "instagram.com" in v:
                    out.add(v)
            else:
                _collect_cdn_urls(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _collect_cdn_urls(x, out)


def _make_graphql_posts_capture(
    save_dir: Path,
) -> tuple[Callable[[Response], None], Callable[[], tuple[list[str], int]]]:
    """Capture GraphQL responses that carry post/media data; return (handler, finalize)."""
    save_dir.mkdir(parents=True, exist_ok=True)
    counter: list[int] = [0]
    seen_hash: set[str] = set()
    all_urls: set[str] = set()

    def on_response(response: Response) -> None:
        if response.status >= 400:
            return
        url = response.url
        if "instagram.com" not in url:
            return
        if "graphql" not in url.lower():
            return
        try:
            text = response.text()
        except Exception:
            return
        raw = text.strip()
        if not raw.startswith("{"):
            return
        if not _graphql_body_looks_like_posts(raw):
            return
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if digest in seen_hash:
            return
        seen_hash.add(digest)
        counter[0] += 1
        path = save_dir / f"posts_graphql_{counter[0]:04d}.json"
        path.write_text(raw, encoding="utf-8")
        try:
            data = json.loads(raw)
            _collect_cdn_urls(data, all_urls)
        except json.JSONDecodeError:
            pass

    def finalize() -> tuple[list[str], int]:
        return sorted(all_urls), counter[0]

    return on_response, finalize


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
    raw_dir = TEMP_DOWNLOAD_RAW / u
    clean_dir = TEMP_DOWNLOAD / u
    clean_dir.mkdir(parents=True, exist_ok=True)
    gql_handler, finalize = _make_graphql_posts_capture(raw_dir)
    page.on("response", gql_handler)
    try:
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
        _scroll_profile_down(page, times=10)
    finally:
        page.remove_listener("response", gql_handler)

    urls, n_saved = finalize()
    bundle = structured_bundle_from_raw_dir(raw_dir, u)
    (clean_dir / "posts.json").write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (clean_dir / "openable_media_urls.json").write_text(
        json.dumps(urls, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        f"  {u}: raw posts_graphql × {n_saved} → {raw_dir.relative_to(PROJECT_ROOT)}, "
        f"posts.json × {len(bundle['posts'])}, openable_media_urls × {len(urls)}"
    )


def _scroll_profile_down(page, times: int) -> None:
    """Scroll the profile page down a few times to load more content."""
    for _ in range(times):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(1000)


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
