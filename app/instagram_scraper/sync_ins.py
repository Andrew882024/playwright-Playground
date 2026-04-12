"""
Open Instagram in Playwright using cookies from .env (see translate_instagram_cookie_into_playwright_version.py).

Visits each account in tempdata.py via the Following modal, scrolls the profile, and records:

- Raw GraphQL bodies: temp_download_raw/<user>/posts_graphql_*.json
- Clean scrape bundle: temp_download/<user>/posts.json (title, comments[] with kind caption|reply,
  taken_at, posting_time_utc, author, links) plus openable_media_urls.json. Each post's openable_media_urls is
  {"largest": [...], "other": [...]} — one best-resolution URL per asset, then remaining variants.

Profile-scroll GraphQL often includes only a preview of comments; comment_count_total and
comments_incomplete flag when the full thread was not loaded (full threads need pagination / post view).

Uses a persistent Firefox profile; screenshot → test_Instagram/.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

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

# Early-exit profile scroll: stop when len(known ∩ this_run) > OVERLAP_STOP_THRESHOLD (e.g. 3+ matches).
OVERLAP_STOP_THRESHOLD = 2
MAX_PROFILE_SCROLLS = 10

# Following modal: scroll down to search for a username row; max scrolls before skipping that target.
MAX_FOLLOWING_MODAL_SCROLLS = 20

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


def _load_known_shortcodes_from_posts_json(path: Path) -> set[str]:
    """Shortcodes from a prior posts.json scrape; empty set if missing or invalid."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    posts = data.get("posts")
    if not isinstance(posts, list):
        return set()
    out: set[str] = set()
    for p in posts:
        if not isinstance(p, dict):
            continue
        sc = p.get("shortcode")
        if isinstance(sc, str) and sc:
            out.add(sc)
    return out


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


def _as_positive_int_unix(val: Any) -> int | None:
    if val is None:
        return None
    try:
        i = int(val)
    except (TypeError, ValueError):
        return None
    if i > 0:
        return i
    return None


def _post_timestamp_unix(node: dict[str, Any]) -> int | None:
    """Unix seconds for post time; prefers taken_at, then device_timestamp if present."""
    for key in ("taken_at", "device_timestamp"):
        v = _as_positive_int_unix(node.get(key))
        if v is not None:
            return v
    return None


def _comment_count_from_node(node: dict[str, Any]) -> int | None:
    emc = node.get("edge_media_to_comment")
    if isinstance(emc, dict):
        c = emc.get("count")
        if isinstance(c, int) and c >= 0:
            return c
    return None


def _comment_record_from_graphql_node(n: dict[str, Any]) -> dict[str, Any]:
    text = n.get("text")
    if not isinstance(text, str):
        text = ""
    owner = n.get("owner")
    username = ""
    if isinstance(owner, dict):
        u = owner.get("username")
        if isinstance(u, str):
            username = u
    cid = n.get("id")
    created = n.get("created_at")
    return {
        "kind": "reply",
        "id": str(cid) if cid is not None else None,
        "text": text,
        "username": username,
        "created_at": created,
    }


def _caption_comment_entry(username: str | None, text: str | None) -> dict[str, Any]:
    u = (username or "").strip() if isinstance(username, str) else ""
    t = text if isinstance(text, str) else ""
    return {"kind": "caption", "text": t, "username": u}


def _comments_unified_from_node(node: dict[str, Any]) -> list[dict[str, Any]]:
    author = _from_username(node)
    cap = _post_caption_text(node)
    caption_entry = _caption_comment_entry(author, cap)
    raw = _raw_comment_nodes_from_media(node)
    reply_list: list[dict[str, Any]] = []
    for n in raw:
        reply_list.append(_comment_record_from_graphql_node(n))
    merged_replies = _merge_comment_lists(reply_list, [])
    return [caption_entry] + merged_replies


def _merge_caption_entries(
    a: dict[str, Any] | None,
    b: dict[str, Any] | None,
    fallback_username: str | None,
) -> dict[str, Any]:
    if not a and not b:
        return _caption_comment_entry(fallback_username, None)
    if not b:
        return a  # type: ignore[return-value]
    if not a:
        return b  # type: ignore[return-value]
    ta = (a.get("text") or "").strip()
    tb = (b.get("text") or "").strip()
    if ta and not tb:
        return a
    if tb and not ta:
        return b
    if ta and tb:
        return a if len(ta) >= len(tb) else b
    ua = (a.get("username") or "").strip() or (fallback_username or "")
    return _caption_comment_entry(ua or None, ta or tb or None)


def _merge_unified_comment_lists(
    prev_list: object,
    rec_list: object,
    *,
    fallback_username: str | None,
) -> list[dict[str, Any]]:
    pl = prev_list if isinstance(prev_list, list) else []
    rl = rec_list if isinstance(rec_list, list) else []
    pc = next((x for x in pl if isinstance(x, dict) and x.get("kind") == "caption"), None)
    rc = next((x for x in rl if isinstance(x, dict) and x.get("kind") == "caption"), None)
    merged_cap = _merge_caption_entries(
        pc if isinstance(pc, dict) else None,
        rc if isinstance(rc, dict) else None,
        fallback_username,
    )
    pr = [x for x in pl if isinstance(x, dict) and x.get("kind") != "caption"]
    rr = [x for x in rl if isinstance(x, dict) and x.get("kind") != "caption"]
    merged_replies = _merge_comment_lists(pr, rr)
    for r in merged_replies:
        if isinstance(r, dict):
            r["kind"] = "reply"
    return [merged_cap] + merged_replies


def _raw_comment_nodes_from_media(node: dict[str, Any]) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    emc = node.get("edge_media_to_comment")
    if isinstance(emc, dict):
        edges = emc.get("edges")
        if isinstance(edges, list):
            for edge in edges:
                if isinstance(edge, dict):
                    cn = edge.get("node")
                    if isinstance(cn, dict):
                        raw.append(cn)
    pc = node.get("preview_comments")
    if isinstance(pc, list):
        for item in pc:
            if not isinstance(item, dict):
                continue
            inner = item.get("node")
            if isinstance(inner, dict):
                raw.append(inner)
            elif "text" in item or item.get("id") is not None:
                raw.append(item)
    return raw


def _comment_dedupe_key(c: dict[str, Any]) -> str:
    cid = c.get("id")
    if cid is not None and str(cid).strip():
        return f"id:{cid}"
    u = str(c.get("username") or "")
    t = str(c.get("text") or "")[:400]
    ct = c.get("created_at")
    payload = f"{ct}|{u}|{t}"
    return f"h:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _merge_comment_lists(a: object, b: object) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for lst in (a if isinstance(a, list) else [], b if isinstance(b, list) else []):
        for c in lst:
            if not isinstance(c, dict):
                continue
            k = _comment_dedupe_key(c)
            if k not in by_key:
                by_key[k] = c

    def _sort_key(x: dict[str, Any]) -> tuple[int, str]:
        ts = x.get("created_at")
        ti = _as_positive_int_unix(ts)
        if ti is None:
            try:
                ti = int(ts) if ts is not None else 0
            except (TypeError, ValueError):
                ti = 0
        return (ti, str(x.get("username") or ""))

    return sorted(by_key.values(), key=_sort_key)


def _merge_optional_comment_count(a: object, b: object) -> int | None:
    ai = a if isinstance(a, int) and a >= 0 else None
    bi = b if isinstance(b, int) and b >= 0 else None
    if ai is None:
        return bi
    if bi is None:
        return ai
    return max(ai, bi)


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


def _instagram_url_asset_key(url: str) -> str:
    """Group CDN variants of the same image (ig_cache_key) or same path filename."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    keys = qs.get("ig_cache_key")
    if keys:
        return f"cache:{keys[0]}"
    path = parsed.path.rstrip("/")
    fname = path.rsplit("/", 1)[-1] if path else url
    return f"path:{fname}"


def _instagram_url_pixel_score(url: str) -> int:
    """Higher means larger display size; used to pick one URL per asset."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    stp_parts = qs.get("stp", [])
    if not stp_parts:
        return 10_000_000
    stp = stp_parts[0]
    best = 0
    for m in re.finditer(r"[sp](\d+)x(\d+)", stp):
        w, h = int(m.group(1)), int(m.group(2))
        best = max(best, w * h)
    if best == 0:
        return 9_000_000
    return best


def _coerce_url_set(val: object) -> set[str]:
    if isinstance(val, set):
        return {str(x) for x in val}
    if isinstance(val, list):
        return {str(x) for x in val}
    return set()


def _organize_instagram_cdn_urls(urls: Iterable[str]) -> dict[str, list[str]]:
    """Per distinct asset: put the largest variant in largest; all other variants in other."""
    url_list = sorted(set(urls))
    by_key: dict[str, list[str]] = {}
    for u in url_list:
        by_key.setdefault(_instagram_url_asset_key(u), []).append(u)
    first_key_order: list[str] = []
    seen: set[str] = set()
    for u in url_list:
        k = _instagram_url_asset_key(u)
        if k not in seen:
            seen.add(k)
            first_key_order.append(k)
    largest: list[str] = []
    other: list[str] = []
    for k in first_key_order:
        group = by_key[k]
        winner = max(group, key=_instagram_url_pixel_score)
        largest.append(winner)
        for u in sorted(group):
            if u != winner:
                other.append(u)
    return {"largest": largest, "other": other}


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
        "comments": _comments_unified_from_node(node),
        "comment_count_total": _comment_count_from_node(node),
        "openable_media_urls": urls,
        "taken_at": _post_timestamp_unix(node),
        "media_pk": str(node["pk"]) if node.get("pk") is not None else None,
    }
    if code not in by_shortcode:
        by_shortcode[code] = rec
        return
    prev = by_shortcode[code]
    merged = _coerce_url_set(prev["openable_media_urls"]) | urls
    prev["openable_media_urls"] = merged
    if prev.get("title") is None and rec.get("title") is not None:
        prev["title"] = rec["title"]
    if prev.get("from_username") is None and rec.get("from_username") is not None:
        prev["from_username"] = rec["from_username"]
    if prev.get("taken_at") is None and rec.get("taken_at") is not None:
        prev["taken_at"] = rec["taken_at"]
    mct = _merge_optional_comment_count(prev.get("comment_count_total"), rec.get("comment_count_total"))
    if mct is not None:
        prev["comment_count_total"] = mct
    author = prev.get("from_username") or rec.get("from_username")
    author_s = author if isinstance(author, str) else None
    prev["comments"] = _merge_unified_comment_lists(
        prev.get("comments"),
        rec.get("comments"),
        fallback_username=author_s,
    )


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
    for p in posts:
        raw = p.get("openable_media_urls")
        p["openable_media_urls"] = _organize_instagram_cdn_urls(_coerce_url_set(raw))
        ts = _as_positive_int_unix(p.get("taken_at"))
        if ts is not None:
            p["taken_at"] = ts
            p["posting_time_utc"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        else:
            p.pop("posting_time_utc", None)
        clist = p.get("comments")
        if not isinstance(clist, list):
            clist = []
        has_caption_kind = any(
            isinstance(x, dict) and x.get("kind") == "caption" for x in clist
        )
        leg_cap = p.get("caption") or p.get("comment")
        leg_s = leg_cap.strip() if isinstance(leg_cap, str) else ""
        if leg_s and not has_caption_kind:
            auth = (p.get("from_username") or "").strip()
            clist = [_caption_comment_entry(auth or None, leg_s)] + clist
        for x in clist:
            if isinstance(x, dict) and "kind" not in x:
                x["kind"] = "reply"
        p["comments"] = clist
        p.pop("caption", None)
        p.pop("comment", None)
        total = p.get("comment_count_total")
        n_replies = sum(
            1 for x in clist if isinstance(x, dict) and x.get("kind") != "caption"
        )
        if isinstance(total, int) and n_replies < total:
            p["comments_incomplete"] = True
        else:
            p.pop("comments_incomplete", None)
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
    shortcodes_out: set[str],
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
            for node in _extract_timeline_nodes(data):
                code = node.get("code")
                if isinstance(code, str) and code:
                    shortcodes_out.add(code)
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


def _open_following_list(page, profile_username: str) -> None:
    """Open the Following list modal from the given account's profile page.

    After visiting another profile, the browser may not be on ``profile_username``'s page, so we
    always navigate to ``/{profile_username}/`` first, then click Following (several fallbacks).
    """
    u = profile_username.lstrip("@").strip()
    profile_home = f"{INSTAGRAM_ORIGIN}/{u}/"
    page.goto(profile_home, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1500)
    _dismiss_instagram_notification_prompt_if_present(page)

    opened = False
    try:
        page.locator(f'a[href="/{u}/following/"]').first.click(timeout=12_000)
        opened = True
    except Exception:
        pass
    if not opened:
        try:
            page.locator('a[href*="/following"]').first.click(timeout=12_000)
            opened = True
        except Exception:
            pass
    if not opened:
        try:
            page.get_by_role("link", name=re.compile(r"following", re.I)).first.click(timeout=12_000)
            opened = True
        except Exception:
            pass
    if not opened:
        try:
            page.locator("span").filter(has_text=re.compile(r"following", re.I)).first.click(timeout=10_000)
            opened = True
        except Exception:
            pass
    if not opened:
        try:
            page.get_by_text(re.compile(r"^\s*following\s*$", re.I)).first.click(timeout=8_000)
            opened = True
        except Exception:
            pass
    if not opened:
        raise RuntimeError(
            f"Could not open Following list from profile {u!r}. "
            "Check that the stats row is visible or the UI changed."
        )

    page.wait_for_timeout(1500)
    try:
        page.locator('[role="dialog"]').first.wait_for(state="visible", timeout=10_000)
    except Exception:
        pass


def _scroll_following_modal_once(page, dialog) -> None:
    """Scroll the Following list inside the dialog (not the main window)."""
    dialog.evaluate(
        """(root) => {
          function findScrollable(el, depth) {
            if (!el || depth > 16) return null;
            const st = window.getComputedStyle(el);
            const oy = st.overflowY;
            if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay') && el.scrollHeight > el.clientHeight + 24) {
              return el;
            }
            for (let i = 0; i < el.children.length; i++) {
              const f = findScrollable(el.children[i], depth + 1);
              if (f) return f;
            }
            return null;
          }
          const sc = findScrollable(root, 0) || root;
          const ch = sc.clientHeight || 400;
          sc.scrollTop += Math.max(200, Math.floor(ch * 0.78));
        }"""
    )
    page.wait_for_timeout(random.randint(350, 700))


def _try_click_following_row_in_modal(page, dialog, username: str) -> bool:
    """Find `a[href="/user/"]` in the Following modal, scrolling up to MAX_FOLLOWING_MODAL_SCROLLS times."""
    u = username.lstrip("@").strip()
    sel = f'a[href="/{u}/"]'
    for attempt in range(MAX_FOLLOWING_MODAL_SCROLLS + 1):
        rows = dialog.locator(sel)
        try:
            if rows.count() > 0:
                row_link = rows.first
                row_link.scroll_into_view_if_needed(timeout=10_000)
                try:
                    row_link.click(timeout=12_000)
                except Exception:
                    try:
                        row_link.click(timeout=12_000, force=True)
                    except Exception:
                        row_link.evaluate("el => el.click()")
                return True
        except Exception:
            pass
        if attempt < MAX_FOLLOWING_MODAL_SCROLLS:
            _scroll_following_modal_once(page, dialog)
    return False


def _open_following_profile(page, username: str) -> None:
    """Click the account row inside the open Following modal (not a global .first link — sidebar can be disabled)."""
    u = username.lstrip("@").strip()
    raw_dir = TEMP_DOWNLOAD_RAW / u
    clean_dir = TEMP_DOWNLOAD / u
    clean_dir.mkdir(parents=True, exist_ok=True)
    known_shortcodes = _load_known_shortcodes_from_posts_json(clean_dir / "posts.json")
    shortcodes_this_run: set[str] = set()
    gql_handler, finalize = _make_graphql_posts_capture(raw_dir, shortcodes_this_run)
    page.on("response", gql_handler)
    scroll_stopped_early = False
    overlap_at_stop = 0
    try:
        dialog = page.locator('[role="dialog"]').first
        dialog.wait_for(state="visible", timeout=15_000)
        # Only links inside the modal; scroll the list if the row is below the fold.
        if not _try_click_following_row_in_modal(page, dialog, u):
            print(
                f"  {u}: not found in Following list after {MAX_FOLLOWING_MODAL_SCROLLS} scrolls, skipping.",
                file=sys.stderr,
            )
            return
        page.wait_for_url(lambda url: f"/{u}/" in url, timeout=25_000)
        page.wait_for_timeout(1500)
        for _ in range(MAX_PROFILE_SCROLLS):
            _scroll_profile_down_once(page)
            overlap_at_stop = len(known_shortcodes & shortcodes_this_run)
            if overlap_at_stop > OVERLAP_STOP_THRESHOLD:
                scroll_stopped_early = True
                break
    finally:
        page.remove_listener("response", gql_handler)

    urls, n_saved = finalize()
    bundle = structured_bundle_from_raw_dir(raw_dir, u)
    organized_all = _organize_instagram_cdn_urls(urls)
    (clean_dir / "posts.json").write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (clean_dir / "openable_media_urls.json").write_text(
        json.dumps(organized_all, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    n_flat = len(organized_all["largest"]) + len(organized_all["other"])
    exit_note = (
        f", early_exit overlap={overlap_at_stop}"
        if scroll_stopped_early
        else f", scrolls={MAX_PROFILE_SCROLLS} (no early exit)"
    )
    print(
        f"  {u}: raw posts_graphql × {n_saved} → {raw_dir.relative_to(PROJECT_ROOT)}, "
        f"posts.json × {len(bundle['posts'])}, openable_media_urls × {n_flat} "
        f"({len(organized_all['largest'])} largest / {len(organized_all['other'])} other)"
        f"{exit_note}"
    )


_ONE_HUMAN_SCROLL_JS = """
async () => {
    const vh = window.innerHeight;
    const doc = document.documentElement;
    const maxScroll = Math.max(0, doc.scrollHeight - vh);
    const start = window.scrollY;
    const room = maxScroll - start;
    if (room <= 2) {
        return { atBottom: true, durationMs: 0 };
    }
    const lo = 0.7 * vh;
    const hi = 1.0 * vh;
    let total = lo + Math.random() * (hi - lo);
    total = Math.min(total, room);
    total = Math.max(1, total);

    const durationMs = 700 + Math.random() * 900;
    const easeInOutQuint = (t) =>
        t < 0.5 ? 16 * t * t * t * t * t : 1 - Math.pow(-2 * t + 2, 5) / 2;

    await new Promise((resolve) => {
        const t0 = performance.now();
        function frame(now) {
            const u = Math.min(1, (now - t0) / durationMs);
            const eased = easeInOutQuint(u);
            window.scrollTo(0, start + total * eased);
            if (u < 1) {
                requestAnimationFrame(frame);
            } else {
                resolve();
            }
        }
        requestAnimationFrame(frame);
    });
    return { atBottom: false, durationMs: Math.round(durationMs) };
}
"""


def _scroll_profile_down_once(page) -> None:
    """One human-like scroll: 70–100% of viewport, slow→fast→slow easing."""
    result = page.evaluate(_ONE_HUMAN_SCROLL_JS)
    if isinstance(result, dict) and result.get("atBottom"):
        page.wait_for_timeout(random.randint(500, 1_400))
        return
    page.wait_for_timeout(random.randint(900, 2_800))


def _scroll_profile_down(page, times: int) -> None:
    """Each scroll: 70–100% of viewport (not tiny chunks), one motion with slow→fast→slow easing."""
    for _ in range(times):
        _scroll_profile_down_once(page)


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
                _open_following_list(page, profile_user)
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
