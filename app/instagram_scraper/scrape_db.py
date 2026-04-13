"""Persist Instagram scrape bundles to PostgreSQL (instagram_posts)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from Blueprint_db import InstagramPosts, SessionLocal  # noqa: E402

_UQ = "uq_instagram_posts_profile_username_post_shortcode"


def known_shortcodes_for_profile(profile_username: str) -> set[str]:
    """Shortcodes already stored for this scraped profile (overlap detection)."""
    u = profile_username.lstrip("@").strip()
    with SessionLocal() as session:
        rows = session.execute(
            select(InstagramPosts.post_shortcode).where(
                InstagramPosts.profile_username == u
            )
        ).all()
    return {str(r[0]) for r in rows if r[0]}


def _caption_text_from_post(p: dict[str, Any]) -> str | None:
    clist = p.get("comments")
    if not isinstance(clist, list):
        return None
    for x in clist:
        if isinstance(x, dict) and x.get("kind") == "caption":
            t = x.get("text")
            if isinstance(t, str) and t.strip():
                return t.strip()
    return None


def _posted_time_from_post(p: dict[str, Any]) -> datetime | None:
    iso = p.get("posting_time_utc")
    if isinstance(iso, str) and iso.strip():
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    ts = p.get("taken_at")
    if isinstance(ts, int) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def _comments_json_payload(p: dict[str, Any]) -> dict[str, Any]:
    clist = p.get("comments")
    if not isinstance(clist, list):
        clist = []
    out: dict[str, Any] = {"comments": clist}
    if p.get("comment_count_total") is not None:
        out["comment_count_total"] = p.get("comment_count_total")
    if p.get("comments_incomplete"):
        out["comments_incomplete"] = True
    return out


def _additional_image_urls_payload(p: dict[str, Any]) -> dict[str, Any] | None:
    """Carousel / extra CDN URLs from scrape. main_image_url is not set here — only after AI."""
    omu = p.get("openable_media_urls")
    if isinstance(omu, dict):
        largest = omu.get("largest")
        other = omu.get("other")
        if isinstance(largest, list) and largest:
            return {
                "largest": largest,
                "other": other if isinstance(other, list) else [],
            }
        if largest == [] and isinstance(other, list) and other:
            return {
                "largest": [],
                "other": other,
            }
    return None


def _row_values(profile_username: str, p: dict[str, Any]) -> dict[str, Any]:
    sc = p.get("shortcode")
    link = p.get("permalink")
    if not isinstance(sc, str) or not sc.strip():
        raise ValueError("post missing shortcode")
    if not isinstance(link, str) or not link.strip():
        link = f"https://www.instagram.com/p/{sc}/"
    extra = _additional_image_urls_payload(p)
    pk = p.get("media_pk")
    ta = p.get("taken_at")
    posted_unix = int(ta) if isinstance(ta, int) else None
    title = p.get("title")
    return {
        "profile_username": profile_username.lstrip("@").strip(),
        "post_shortcode": sc.strip(),
        "post_url": link.strip(),
        "post_title": title if isinstance(title, str) else None,
        "posted_unix_seconds": posted_unix,
        "posted_time": _posted_time_from_post(p),
        "instagram_media_id": str(pk) if pk is not None else None,
        "caption": _caption_text_from_post(p),
        "comments_json": _comments_json_payload(p),
        "main_image_url": None,
        "additional_image_urls": extra,
    }


def upsert_posts_for_profile(profile_username: str, posts: list[dict[str, Any]]) -> int:
    """Insert or update scrape columns for each post.

    On conflict, refreshes caption/media metadata but does not set ``main_image_url`` (that
    column is left null on insert and is not overwritten on update — use AI output to fill it).
    """
    pn = profile_username.lstrip("@").strip()
    n = 0
    with SessionLocal() as session:
        for p in posts:
            if not isinstance(p, dict):
                continue
            try:
                vals = _row_values(pn, p)
            except ValueError:
                continue
            stmt = insert(InstagramPosts).values(**vals)
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                constraint=_UQ,
                set_={
                    InstagramPosts.post_url: ex.post_url,
                    InstagramPosts.post_title: ex.post_title,
                    InstagramPosts.posted_unix_seconds: ex.posted_unix_seconds,
                    InstagramPosts.posted_time: ex.posted_time,
                    InstagramPosts.instagram_media_id: ex.instagram_media_id,
                    InstagramPosts.caption: ex.caption,
                    InstagramPosts.comments_json: ex.comments_json,
                    InstagramPosts.additional_image_urls: ex.additional_image_urls,
                },
            )
            session.execute(stmt)
            n += 1
        session.commit()
    return n
