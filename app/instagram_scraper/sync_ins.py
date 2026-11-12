"""
Open Instagram in Playwright using instagram_cookies_firefox.json (see translate_instagram_cookie_into_playwright_version.py).

Reads ``TARGET_URLS`` from tempdata.py: each bare username is opened in turn, scrolled to collect
timeline GraphQL, and posts are upserted into PostgreSQL
(``instagram_posts``). GraphQL is kept in
memory only (no temp_download / temp_download_raw).

Each post's openable_media_urls in the structured bundle is {"largest": [...], "other": [...]} —
only URLs from the post's own media (not profile/avatar images from user or comment objects).

Profile-scroll GraphQL often includes only a preview of comments; comment_count_total and
comments_incomplete flag when the full thread was not loaded (full threads need pagination / post view).

Uses a persistent Firefox profile; screenshot → test_Instagram/.
"""

from __future__ import annotations

import argparse
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
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from app.instagram_scraper.recency import active_cutoff, recent_post, OldPostStopper
from app.instagram_scraper.timeline_media import resolve_timeline_media
from app.instagram_scraper.graphql_diagnostics import record_errors, record_unsupported, record_error_response, aggregate_diagnostics
from app.instagram_scraper.scrape_health import (
    page_failure, new_capture_health, classify_timeline, capture_failure, capture_error, browser_closed,
    CAPTURE_ERROR_CATEGORIES,
)
from tempdata import TARGET_URLS  # noqa: E402

def known_shortcodes_for_profile(profile_username):
    # Persistence is loaded only for normal scrape execution, never diagnostics.
    from scrape_db import known_shortcodes_for_profile as read_known
    return read_known(profile_username)


def upsert_posts_for_profile(*args, **kwargs):
    from scrape_db import upsert_posts_for_profile as persist
    return persist(*args, **kwargs)


SCREENSHOT_DIR = PROJECT_ROOT / "test_Instagram"
# Same folder each run = Firefox remembers cookies, localStorage, and notification permission for the origin.
# grant_permissions() + one in-page click (below) match site settings + Instagram’s own “already answered” state.
PERSISTENT_PROFILE_DIR = PROJECT_ROOT / ".playwright_instagram_profile"
INSTAGRAM_ORIGIN = "https://www.instagram.com"

# Early-exit profile scroll: stop when len(known ∩ this_run) > OVERLAP_STOP_THRESHOLD (e.g. 3+ matches).
OVERLAP_STOP_THRESHOLD = 2
MAX_PROFILE_SCROLLS = 10

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

# JSON keys whose string values are usually post image/video URLs (not account avatars).
_MEDIA_URL_KEYS = frozenset(
    {
        "display_url",
        "video_url",
        "thumbnail_src",
        "url",
    }
)

# Never treat these as post media (account avatars, header art, etc.).
_CDN_URL_KEY_EXCLUDES = frozenset(
    {
        "profile_pic_url",
        "hd_profile_pic_url",
        "profile_pic_url_hd",
        "borderless_profile_pic_url",
        "delegate_page_profile_pic_url",
    }
)


def _cdn_url_key_excluded(key: str) -> bool:
    k = key.lower()
    if key in _CDN_URL_KEY_EXCLUDES:
        return True
    if "profile_pic" in k or k.endswith("_profile_pic_url"):
        return True
    if "avatar" in k and "_url" in k:
        return True
    return False


def _add_if_instagram_cdn(val: object, out: set[str]) -> None:
    if not isinstance(val, str) or not val.startswith("https://"):
        return
    if "cdninstagram" in val or "fbcdn.net" in val or "instagram.com" in val:
        out.add(val)


def _collect_urls_from_image_versions_tree(obj: object, out: set[str]) -> None:
    """image_versions2 / nested candidates use dicts with a url field."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url":
                _add_if_instagram_cdn(v, out)
            else:
                _collect_urls_from_image_versions_tree(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _collect_urls_from_image_versions_tree(x, out)


def _post_media_cdn_urls_from_timeline_node(node: dict[str, Any]) -> set[str]:
    """CDN URLs for the post body only (not owner/commenter profile pictures)."""
    out: set[str] = set()
    _add_if_instagram_cdn(node.get("display_url"), out)
    _add_if_instagram_cdn(node.get("thumbnail_src"), out)
    _add_if_instagram_cdn(node.get("video_url"), out)
    _collect_urls_from_image_versions_tree(node.get("image_versions2"), out)
    _collect_urls_from_image_versions_tree(node.get("image_versions"), out)

    dr = node.get("display_resources")
    if isinstance(dr, list):
        for r in dr:
            if isinstance(r, dict):
                _add_if_instagram_cdn(r.get("src"), out)

    cm = node.get("carousel_media")
    if isinstance(cm, list):
        for item in cm:
            if isinstance(item, dict):
                _add_if_instagram_cdn(item.get("display_url"), out)
                _add_if_instagram_cdn(item.get("thumbnail_src"), out)
                _collect_urls_from_image_versions_tree(item.get("image_versions2"), out)
                _collect_urls_from_image_versions_tree(item.get("image_versions"), out)

    esc = node.get("edge_sidecar_to_children")
    if isinstance(esc, dict):
        edges = esc.get("edges")
        if isinstance(edges, list):
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                child = edge.get("node")
                if isinstance(child, dict):
                    _add_if_instagram_cdn(child.get("display_url"), out)
                    _add_if_instagram_cdn(child.get("thumbnail_src"), out)
                    _collect_urls_from_image_versions_tree(child.get("image_versions2"), out)
                    _collect_urls_from_image_versions_tree(child.get("image_versions"), out)

    vv = node.get("video_versions")
    if isinstance(vv, list):
        for ent in vv:
            if isinstance(ent, dict):
                _add_if_instagram_cdn(ent.get("url"), out)

    return out


def _graphql_body_looks_like_posts(body: str) -> bool:
    return any(m in body for m in _POST_GRAPHQL_MARKERS)


def _extract_timeline_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve only direct nodes and the confirmed node.media wrapper."""
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
            media, _ = resolve_timeline_media(edge)
            if media is not None:
                out.append(media)
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


def _openable_urls_for_post_node(node: dict[str, Any]) -> list[str]:
    """Post-attached media only; excludes account avatars from nested user/comment objects."""
    found = _post_media_cdn_urls_from_timeline_node(node)
    if not found:
        _collect_cdn_urls_excluding_account_images(node, found)
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
    urls = set(_openable_urls_for_post_node(node))
    rec = {
        "shortcode": code,
        "is_pinned": node.get("is_pinned", node.get("is_pinned_to_profile")),
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


def structured_bundle_from_graphql_bodies(
    bodies: list[str], scraped_username: str, *, preserve_order: bool = False, best_effort: bool = False, warnings: list | None = None
) -> dict[str, Any]:
    """Build the same posts bundle as the old posts.json flow, from in-memory GraphQL JSON strings."""
    by_shortcode: dict[str, dict[str, Any]] = {}
    for body_text in bodies:
        try:
            data = json.loads(body_text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for node in _extract_timeline_nodes(data):
            try:
                if best_effort:
                    from copy import deepcopy
                    projected = deepcopy(by_shortcode)
                    _merge_structured_posts(projected, node)
                    by_shortcode = projected
                else:
                    _merge_structured_posts(by_shortcode, node)
            except Exception:
                if not best_effort: raise
                import re
                code = node.get('code')
                code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else None
                if warnings is not None: warnings.append(dict(post_id=code, reason='post_projection_failed'))
    posts = list(by_shortcode.values()) if preserve_order else sorted(
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


def _collect_cdn_urls_excluding_account_images(obj: object, out: set[str]) -> None:
    """Gather Instagram/Facebook CDN media URLs; skip keys used for avatars / profile art."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _cdn_url_key_excluded(k):
                if not isinstance(v, str):
                    _collect_cdn_urls_excluding_account_images(v, out)
                continue
            if isinstance(v, str) and v.startswith("https://") and (
                k in _MEDIA_URL_KEYS
                or (k.endswith("_url") and ("instagram" in v or "fbcdn" in v))
            ):
                if "cdninstagram" in v or "fbcdn.net" in v or "instagram.com" in v:
                    out.add(v)
            else:
                _collect_cdn_urls_excluding_account_images(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _collect_cdn_urls_excluding_account_images(x, out)


def _make_graphql_posts_capture(
    bodies_out: list[str],
    shortcodes_out: set[str],
    health: dict | None = None,
    *, diagnostic_payload: Callable | None = None,
) -> tuple[Callable[[Response], None], Callable[[], tuple[list[str], int]]]:
    """Capture GraphQL responses that carry post/media data; return (handler, finalize)."""
    seen_hash: set[str] = set()
    all_urls: set[str] = set()
    health = health if health is not None else new_capture_health()
    from app.instagram_scraper.schema_probe import capture_observer
    schema_observer = capture_observer()  # Bind opt-in context before browser event dispatch.

    def on_response(response: Response) -> None:
        try:
            url = response.url
            if urlparse(url).hostname not in ('www.instagram.com', 'instagram.com'):
                return
            if "graphql" not in url.lower():
                return
            health['graphql_responses'] += 1
            if response.status >= 400:
                capture_error(health, 'http_error')
                if response.status not in health['http_status_codes']:
                    health['http_status_codes'].append(response.status)
                return
            try:
                text = response.text()
            except Exception as exc:
                capture_error(health, 'browser_context_closed' if browser_closed(exc) else 'response_body_unavailable')
                return
            if not isinstance(text, str):
                capture_error(health, 'unexpected_json_structure')
                return
            raw = text.strip()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                capture_error(health, 'json_decoding_failure')
                return
            if not isinstance(data, dict):
                capture_error(health, 'unexpected_json_structure')
                return
            from app.instagram_scraper.response_policy import unrelated_home_feed, location_avatar_errors_only
            if unrelated_home_feed(response, data):
                health['ignored_graphql_responses'] += 1
                health['ignored_home_feed_responses'] = health.get('ignored_home_feed_responses', 0) + 1
                return
            avatar_errors = bool(data.get('errors')) and location_avatar_errors_only(response, data)
            if avatar_errors:
                # Record the provider failure even though this unused leaf is safe.
                record_errors(health, data['errors'])
                record_error_response(health, data)
                data = {key: value for key, value in data.items() if key != 'errors'}
                health['location_avatar_error_responses'] = health.get('location_avatar_error_responses', 0) + 1
                raw = json.dumps(data)
            if data.get('errors'):
                if diagnostic_payload is not None:
                    diagnostic_payload(data, 'graphql_error')
                if schema_observer is not None:
                    schema_observer(response, data)
                record_errors(health, data['errors'])
                record_error_response(health, data)
                capture_error(health, 'graphql_error')
                return
            if health.get('best_effort'):
                # Salvage individual valid edges only in the known PROFILE connection.
                from app.instagram_scraper.response_policy import PROFILE_ROOT
                connection = data.get('data', {}).get(PROFILE_ROOT)
                if isinstance(connection, dict) and isinstance(connection.get('edges'), list):
                    accepted = []
                    for edge in connection['edges']:
                        media, reason = resolve_timeline_media(edge)
                        if media is None:
                            node = edge.get('node') if isinstance(edge, dict) else None
                            identity = node.get('code') if isinstance(node, dict) else None
                            import re
                            identity = identity if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', identity) else None
                            health.setdefault('post_warnings', []).append(dict(post_id=identity, reason=reason))
                        else:
                            accepted.append(edge)
                    if len(accepted) != len(connection['edges']):
                        if not accepted:
                            capture_error(health, 'unsupported_timeline_schema')
                            return
                        data = dict(data, data=dict(data['data'], **{PROFILE_ROOT: dict(connection, edges=accepted)}))
                        raw = json.dumps(data)
            nodes = _extract_timeline_nodes(data)
            category = classify_timeline(data, nodes, post_data_hint=_graphql_body_looks_like_posts(raw))
            if diagnostic_payload is not None:
                diagnostic_payload(data, category)
            if category == 'ignored_graphql_response':
                health['ignored_graphql_responses'] += 1
                return
            if category not in ('valid_timeline','valid_empty_timeline'):
                if category == 'unsupported_timeline_schema':record_unsupported(health, data)
                capture_error(health, category)
                return
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if digest not in seen_hash:
                urls = set()
                _collect_cdn_urls_excluding_account_images(data, urls)
                codes = {node['code'] for node in nodes}
                # Publish captured data only after all response processing succeeds.
                seen_hash.add(digest)
                bodies_out.append(raw)
                all_urls.update(urls)
                shortcodes_out.update(codes)
            health['valid_timeline_responses'] += 1
            if category == 'valid_empty_timeline':
                health['valid_empty_timeline_responses'] += 1
        except Exception as exc:
            capture_error(health, 'browser_context_closed' if browser_closed(exc) else 'response_processing_exception')

    def finalize() -> tuple[list[str], int]:
        return sorted(all_urls), len(bodies_out)

    return on_response, finalize


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

    await Promise.race([new Promise((resolve) => {
        const t0 = performance.now();
        function frame(now) {
            const u = Math.min(1, (now - t0) / durationMs);
            const eased = easeInOutQuint(u);
            window.scrollTo(0, start + total * eased);
            if (u < 1) {
                setTimeout(() => frame(performance.now()), 16);
            } else {
                resolve();
            }
        }
        setTimeout(() => frame(performance.now()), 16);
    }), new Promise((_, reject) => setTimeout(() => reject(new Error("Scroll timed out")), 10000))]);
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


def _scroll_profile_capture_and_upsert(
    page, profile_username: str, profile_url: str, *, best_effort=False
) -> dict:
    """Navigate to ``profile_url``, capture timeline GraphQL (including the first in-flight batch),
    scroll, and upsert posts.

    The ``response`` listener must be registered *before* ``page.goto`` so the initial
    profile-load GraphQL responses are not dropped.
    """
    cutoff = active_cutoff()
    u = profile_username.lstrip("@").strip()
    known_shortcodes = known_shortcodes_for_profile(u)
    bodies_this_run: list[str] = []
    shortcodes_this_run: set[str] = set()
    health = new_capture_health()
    health['best_effort'] = best_effort
    def failed(reason):
        health.update(status='failed', reason=reason)
        return dict(profile=u, inserted=0, updated=0, unchanged=0, inserted_parent_ids=[],
                    scrape_health=health)
    gql_handler, finalize = _make_graphql_posts_capture(bodies_this_run, shortcodes_this_run, health)
    def on_request_failed(request):
        try:
            parsed = urlparse(request.url)
            if parsed.hostname in ('www.instagram.com', 'instagram.com') and 'graphql' in parsed.path.lower():
                capture_error(health, 'request_failed')
        except Exception as exc:
            capture_error(health, 'browser_context_closed' if browser_closed(exc) else 'response_processing_exception')
    page.on('requestfailed', on_request_failed)
    page.on("response", gql_handler)
    scroll_stopped_early = False
    overlap_at_stop = 0
    try:
        navigation = page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        reason = page_failure(page)
        if reason:
            return failed(reason)
        if navigation is None or navigation.status >= 400:
            if navigation is not None:
                health['http_status_codes'].append(navigation.status)
            return failed('navigation_blocked')
        _dismiss_instagram_notification_prompt_if_present(page)
        page.wait_for_timeout(1500)
        initial = structured_bundle_from_graphql_bodies(bodies_this_run, u, preserve_order=True, best_effort=best_effort, warnings=health.setdefault('post_warnings', []))["posts"]
        stopper = OldPostStopper([p["shortcode"] for p in initial], cutoff)
        for _ in range(MAX_PROFILE_SCROLLS):
            _scroll_profile_down_once(page)
            reason = page_failure(page)
            if reason:
                return failed(reason)
            loaded = structured_bundle_from_graphql_bodies(bodies_this_run, u, preserve_order=True, best_effort=best_effort, warnings=health.setdefault('post_warnings', []))["posts"]
            if stopper.observe(loaded):
                print("  Profile scroll stopped at reliable old-post boundary", flush=True)
                break
            overlap_at_stop = len(known_shortcodes & {p["shortcode"] for p in loaded if recent_post(p, cutoff)})
            if overlap_at_stop > OVERLAP_STOP_THRESHOLD:
                scroll_stopped_early = True
                break
        reason = page_failure(page) or capture_failure(health)
        if reason:
            return failed(reason)
    except Exception as exc:
        # Playwright exceptions may contain URLs or browser output.
        if browser_closed(exc): capture_error(health, 'browser_context_closed')
        return failed('navigation_or_browser_failed')
    finally:
        for event, handler in (('response', gql_handler), ('requestfailed', on_request_failed)):
            try: page.remove_listener(event, handler)
            except Exception as exc:
                capture_error(health, 'browser_context_closed' if browser_closed(exc) else 'response_processing_exception')

    # Cleanup failures must not turn a valid capture into a successful upsert.
    if capture_failure(health): return failed(capture_failure(health))

    urls, n_saved = finalize()
    bundle = structured_bundle_from_graphql_bodies(bodies_this_run, u, best_effort=best_effort, warnings=health.setdefault('post_warnings', []))
    active = [post for post in bundle["posts"] if recent_post(post, cutoff)]
    if best_effort:
        for post in bundle['posts']:
            if post.get('taken_at') is None:
                health.setdefault('post_warnings', []).append(dict(post_id=post.get('shortcode'), reason='missing_publication_timestamp'))
    counts={};inserted_ids=[]
    n_upserted = upsert_posts_for_profile(u, active, report=counts, inserted_parent_ids=inserted_ids, **({'best_effort': True} if best_effort else {}))
    organized_all = _organize_instagram_cdn_urls(urls)
    n_flat = len(organized_all["largest"]) + len(organized_all["other"])
    exit_note = (
        f", early_exit overlap={overlap_at_stop}"
        if scroll_stopped_early
        else f", scrolls={MAX_PROFILE_SCROLLS} (no early exit)"
    )
    print(
        f"  {u}: graphql bodies × {n_saved} → postgres rows upserted × {n_upserted}, "
        f"posts in bundle × {len(bundle['posts'])}, cdn urls × {n_flat} "
        f"({len(organized_all['largest'])} largest / {len(organized_all['other'])} other)"
        f"{exit_note}"
    )

    partial = bool(health['capture_errors'] or health.get('post_warnings') or counts.get('failed') or counts.get('skipped'))
    return dict(profile=u,inserted_parent_ids=inserted_ids,**counts,
                scrape_health=dict(status='partial' if partial else 'healthy', reason='partial_capture' if partial else 'timeline_verified',
                                   captured_posts=len(bundle['posts']), active_posts=len(active), **health))


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


def main(argv: list[str] | None = None) -> dict:
    parser=argparse.ArgumentParser(description='Scrape configured Instagram accounts only; no AI/import stages.')
    parser.add_argument('--best-effort',action='store_true',help='Keep valid posts; report incomplete captures as partial')
    parser.add_argument('--profile',help='Exactly one account from TARGET_URLS')
    parser.add_argument('--headless',action='store_true',help='Run Firefox without a graphical window')
    args=parser.parse_args([] if argv is None else argv)
    profiles=list(TARGET_URLS)
    if args.profile is not None:
        account=args.profile.strip().lstrip('@')
        matches=[profile for profile in profiles if profile.casefold()==account.casefold()]
        if len(matches)!=1:parser.error('--profile must identify exactly one configured Instagram account')
        profiles=matches
    active_cutoff()  # validate configuration before browser operations
    cookies = load_instagram_cookies_for_playwright()
    if not TARGET_URLS:
        raise SystemExit("tempdata.TARGET_URLS is empty; add at least one handle or URL.")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    PERSISTENT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    summaries=[]
    with sync_playwright() as p:
        context = p.firefox.launch_persistent_context(
            str(PERSISTENT_PROFILE_DIR),
            headless=args.headless,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(30_000)
        context.set_default_navigation_timeout(60_000)
        context.grant_permissions(["notifications"], origin=INSTAGRAM_ORIGIN)
        context.add_cookies(cookies)
        # Persistent Firefox already has one startup tab; new_page() would add a second ("Opening 2").
        while len(context.pages) > 1:
            context.pages[-1].close()
        page = context.pages[0] if context.pages else context.new_page()
        screenshot_paths: list[Path] = []
        for profile_user in profiles:
            target_url = f"{INSTAGRAM_ORIGIN}/{profile_user}/"
            summaries.append(_scroll_profile_capture_and_upsert(page, profile_user, target_url, **({'best_effort': True} if args.best_effort else {})))
            if summaries[-1].get('scrape_health', {}).get('status') != 'failed':
                out_path = SCREENSHOT_DIR / f"instagram_{profile_user}.png"
                page.screenshot(path=str(out_path), full_page=True)
                screenshot_paths.append(out_path)
        context.close()

    for pth in screenshot_paths:
        print(f"Screenshot saved to {pth}")
    unhealthy = any(item.get('scrape_health', {}).get('status') == 'failed' for item in summaries)
    partial = any(item.get('scrape_health', {}).get('status') == 'partial' for item in summaries)
    result=dict(status='failed' if unhealthy else 'partial' if partial else 'completed',profiles=summaries,
                scrape_health=dict(status='failed' if unhealthy else 'partial' if partial else 'healthy',
                    **aggregate_diagnostics([item.get('scrape_health',{}) for item in summaries]), profiles=[
                    dict(profile=item['profile'], **item['scrape_health']) for item in summaries if 'scrape_health' in item],
                    error_categories={key:sum(item.get('scrape_health',{}).get('error_categories',{}).get(key,0)
                                              for item in summaries) for key in CAPTURE_ERROR_CATEGORIES},
                    **{key:sum(item.get('scrape_health',{}).get(key,0) for item in summaries) for key in
                       ('capture_errors','graphql_responses','valid_timeline_responses',
                        'valid_empty_timeline_responses','ignored_graphql_responses')}),
                **{key:sum(item.get(key,0) for item in summaries)
                for key in ('inserted','updated','unchanged')})
    print(json.dumps(result,sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(0 if main(sys.argv[1:])['status'] in ('completed', 'partial') else 1)
