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

from app.instagram_scraper.recency import active_cutoff
from Blueprint_db import InstagramPosts, SessionLocal  # noqa: E402

from app.ai_analyze_service.config import ai_analyze_recent_days  # noqa: E402


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
    post_id: int | None = None,
) -> list[InstagramPosts]:
    """Rows for one profile within the recent-days window, optionally excluding ai_analyzed."""
    pn = profile_username.lstrip("@").strip()
    days = ai_analyze_recent_days()
    cutoff = max(active_cutoff(), datetime.now(timezone.utc) - timedelta(days=days))
    stmt = select(InstagramPosts).where(InstagramPosts.profile_username == pn)
    if post_id is not None:
        stmt = stmt.where(InstagramPosts.id == post_id)
    stmt = stmt.where(posted_at_utc_coalesce() >= cutoff)
    if only_not_analyzed:
        stmt = stmt.where(InstagramPosts.ai_analyzed.is_(False))
    stmt = stmt.order_by(
        InstagramPosts.posted_time.desc().nulls_last(),
        InstagramPosts.posted_unix_seconds.desc().nulls_last(),
        InstagramPosts.id.desc(),
    )
    from app.instagram_scraper.parent_binding import production_candidates_only
    stmt = stmt.where(production_candidates_only(InstagramPosts))
    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()
    return list(rows)


def distinct_profile_usernames_recent(
    *, only_not_analyzed: bool = True, post_id: int | None = None,
) -> list[str]:
    """Profiles with at least one instagram_posts row in the recent-days window."""
    days = ai_analyze_recent_days()
    cutoff = max(active_cutoff(), datetime.now(timezone.utc) - timedelta(days=days))
    stmt = (
        select(InstagramPosts.profile_username)
        .where(posted_at_utc_coalesce() >= cutoff)
        .distinct()
        .order_by(InstagramPosts.profile_username)
    )
    if post_id is not None:
        stmt = stmt.where(InstagramPosts.id == post_id)
    if only_not_analyzed:
        stmt = stmt.where(InstagramPosts.ai_analyzed.is_(False))
    from app.instagram_scraper.parent_binding import production_candidates_only
    stmt = stmt.where(production_candidates_only(InstagramPosts))
    with SessionLocal() as session:
        rows = session.execute(stmt).all()
    out: list[str] = []
    for r in rows:
        if r[0] and str(r[0]).strip():
            out.append(str(r[0]).strip())
    return out
