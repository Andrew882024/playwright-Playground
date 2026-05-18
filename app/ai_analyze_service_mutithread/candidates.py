"""Fetch instagram_posts rows eligible for AI analysis (recency + analyzed flag)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

# Repo root (…/playwright-Playground)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from Blueprint_db import InstagramPosts, SessionLocal  # noqa: E402

from app.ai_analyze_service_mutithread.config import ai_analyze_recent_days  # noqa: E402


def posted_at_utc_coalesce():
    """posted_time if set, else Unix epoch as timestamptz UTC (for recency filter)."""
    return func.coalesce(
        InstagramPosts.posted_time,
        func.timezone("UTC", func.to_timestamp(InstagramPosts.posted_unix_seconds)),
    )


def fetch_instagram_rows_for_profile(
    profile_username: str,
    *,
    only_not_analyzed: bool = True,
) -> list[InstagramPosts]:
    """Rows for one profile within the recent-days window, optionally excluding ai_analyzed."""
    pn = profile_username.lstrip("@").strip()
    days = ai_analyze_recent_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(InstagramPosts).where(InstagramPosts.profile_username == pn)
    stmt = stmt.where(posted_at_utc_coalesce() >= cutoff)
    if only_not_analyzed:
        stmt = stmt.where(InstagramPosts.ai_analyzed.is_(False))
    stmt = stmt.order_by(
        InstagramPosts.posted_time.desc().nulls_last(),
        InstagramPosts.posted_unix_seconds.desc().nulls_last(),
        InstagramPosts.id.desc(),
    )
    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()
    return list(rows)


def distinct_profile_usernames_recent(*, only_not_analyzed: bool = True) -> list[str]:
    """Profiles with at least one instagram_posts row in the recent-days window."""
    days = ai_analyze_recent_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(InstagramPosts.profile_username)
        .where(posted_at_utc_coalesce() >= cutoff)
        .distinct()
        .order_by(InstagramPosts.profile_username)
    )
    if only_not_analyzed:
        stmt = stmt.where(InstagramPosts.ai_analyzed.is_(False))
    with SessionLocal() as session:
        rows = session.execute(stmt).all()
    out: list[str] = []
    for r in rows:
        if r[0] and str(r[0]).strip():
            out.append(str(r[0]).strip())
    return out


def fetch_all_candidate_post_keys(
    *,
    only_not_analyzed: bool = True,
    profile_username: str | None = None,
) -> list[tuple[str, str]]:
    """(profile_username, post_shortcode) for every row in the recent-days window."""
    days = ai_analyze_recent_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(
        InstagramPosts.profile_username,
        InstagramPosts.post_shortcode,
    ).where(posted_at_utc_coalesce() >= cutoff)
    if only_not_analyzed:
        stmt = stmt.where(InstagramPosts.ai_analyzed.is_(False))
    if profile_username:
        pn = profile_username.lstrip("@").strip()
        stmt = stmt.where(InstagramPosts.profile_username == pn)
    stmt = stmt.order_by(
        InstagramPosts.profile_username,
        InstagramPosts.posted_time.desc().nulls_last(),
        InstagramPosts.posted_unix_seconds.desc().nulls_last(),
        InstagramPosts.id.desc(),
    )
    with SessionLocal() as session:
        rows = session.execute(stmt).all()
    out: list[tuple[str, str]] = []
    for prof, sc in rows:
        if prof and sc and str(prof).strip() and str(sc).strip():
            out.append((str(prof).strip(), str(sc).strip()))
    return out


def fetch_instagram_row(profile_username: str, post_shortcode: str) -> InstagramPosts | None:
    """Single row by natural key, or None."""
    pn = profile_username.lstrip("@").strip()
    sc = post_shortcode.strip()
    stmt = select(InstagramPosts).where(
        InstagramPosts.profile_username == pn,
        InstagramPosts.post_shortcode == sc,
    )
    with SessionLocal() as session:
        return session.execute(stmt).scalar_one_or_none()
