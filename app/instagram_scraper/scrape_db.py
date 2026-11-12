"""Persist Instagram scrape bundles to PostgreSQL (instagram_posts)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, unquote

from sqlalchemy import select, update
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


# Explicit allowlist: never assign AI, selected image, cover or import metadata.
SCRAPE_COLUMNS = ('post_url','post_title','posted_unix_seconds','posted_time',
                  'instagram_media_id','caption','comments_json','additional_image_urls')


def _cdn_asset(value):
    if not isinstance(value,str):return value
    try:
        parsed=urlsplit(value);host=(parsed.hostname or '').lower()
        if host.endswith('.cdninstagram.com') or host=='cdninstagram.com' or host.endswith('.fbcdn.net') or host=='fbcdn.net':
            return ('cdn_asset',unquote(parsed.path.rsplit('/',1)[-1]))
    except ValueError:pass
    return value


def _canonical(value):
    if isinstance(value,datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value,dict):return {k:_canonical(v) for k,v in value.items()}
    if isinstance(value,list):return [_canonical(v) for v in value]
    return _cdn_asset(value)


def meaningful_values(values):
    result={key:_canonical(values.get(key)) for key in SCRAPE_COLUMNS if key!='additional_image_urls'}
    images=values.get('additional_image_urls') or {}
    # Different CDN variants/ordering for the same assets are not content edits.
    if isinstance(images,dict):
        result['additional_image_urls']=sorted({str(_cdn_asset(url)) for key in ('largest','other')
            for url in (images.get(key) or []) if isinstance(url,str)})
    else:result['additional_image_urls']=_canonical(images)
    return result


def upsert_posts_for_profile(profile_username: str, posts: list[dict[str, Any]], *, report: dict | None = None, inserted_parent_ids: list | None = None, best_effort: bool = False) -> int:
    """Atomic insert-or-locked-update, classified once per distinct shortcode.

    Counts reflect committed scrape data. CDN-only refreshes count unchanged but
    can still refresh stored media references. Existing IDs/AI fields are retained.
    """
    if best_effort:
        from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
        import re
        counts = dict(inserted=0, updated=0, unchanged=0, failed=0, skipped=0, warnings=[])
        seen = set()
        for post in posts:
            identity = post.get('shortcode') if isinstance(post, dict) else None
            identity = identity if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', identity) else None
            if identity and identity in seen:
                continue
            if identity: seen.add(identity)
            try:
                if not isinstance(post, dict): raise ValueError()
                values = _row_values(profile_username, post)
                if values['posted_time'] is None or not values['additional_image_urls']:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                counts['skipped'] += 1
                counts['warnings'].append(dict(post_id=identity, reason='missing_or_invalid_post_fields'))
                continue
            try:
                one = {}; ids = []
                # One existing transaction per post: later rollback cannot undo earlier commits.
                upsert_posts_for_profile(profile_username, [post], report=one, inserted_parent_ids=ids)
                for key in ('inserted', 'updated', 'unchanged'): counts[key] += one[key]
                if inserted_parent_ids is not None: inserted_parent_ids.extend(ids)
            except (OperationalError, InterfaceError):
                raise
            except Exception as exc:
                if isinstance(exc, DBAPIError) and exc.connection_invalidated: raise
                counts['failed'] += 1
                counts['warnings'].append(dict(post_id=identity, reason='post_transaction_failed'))
        if report is not None: report.update(counts)
        return counts['inserted'] + counts['updated'] + counts['unchanged']
    pn=profile_username.lstrip('@').strip()
    counts=dict(inserted=0,updated=0,unchanged=0)
    inserted_ids=[]
    prepared={}
    for post in posts:
        if not isinstance(post,dict):continue
        try:values=_row_values(pn,post)
        except ValueError:continue
        prepared[values['post_shortcode']]=values  # Same identity counted only once.
    with SessionLocal() as session:
        for shortcode,values in sorted(prepared.items()):
            inserted_id=session.execute(insert(InstagramPosts).values(**values).on_conflict_do_nothing(
                constraint=_UQ).returning(InstagramPosts.id)).scalar_one_or_none()
            if inserted_id is not None:
                inserted_ids.append(inserted_id)
                counts['inserted']+=1
                continue
            existing=session.execute(select(InstagramPosts).where(
                InstagramPosts.profile_username==pn,InstagramPosts.post_shortcode==shortcode)
                .with_for_update()).scalar_one()
            old={key:getattr(existing,key) for key in SCRAPE_COLUMNS}
            if isinstance(old["comments_json"], dict) and "analysis_review" in old["comments_json"]:
                values["comments_json"]["analysis_review"] = old["comments_json"]["analysis_review"]
            changed=meaningful_values(old)!=meaningful_values(values)
            counts['updated' if changed else 'unchanged']+=1
            replacements={key:values[key] for key in SCRAPE_COLUMNS if _canonical(old[key])!=_canonical(values[key])
                          or (key=='additional_image_urls' and old[key]!=values[key])}
            if replacements:
                session.execute(update(InstagramPosts).where(InstagramPosts.id==existing.id).values(**replacements))
        session.commit()
    if inserted_parent_ids is not None:inserted_parent_ids.extend(inserted_ids)
    if report is not None:report.update(counts)
    return sum(counts.values())


def insert_analysis_parent(profile_username, post, review):
    """Explicit review handoff: insert-only using the scraper's existing mapping.

    Caller holds canonical-shortcode advisory lock. Existing parents, AI columns,
    IDs and import markers are never updated by this operation.
    """
    from app.instagram_scraper.parent_binding import REVIEW_KEY
    if (review.get('scope') != 'local_analysis_only' or review.get('production_eligible') is not False
            or review.get('import_authorized') is not False):
        raise ValueError('Analysis-only quarantine metadata required')
    values = _row_values(profile_username, post)
    values['comments_json'][REVIEW_KEY] = review
    with SessionLocal() as session:
        parent_id = session.execute(insert(InstagramPosts).values(**values).on_conflict_do_nothing(
            constraint=_UQ).returning(InstagramPosts.id)).scalar_one_or_none()
        inserted = parent_id is not None
        if parent_id is None:
            parent_id = session.execute(select(InstagramPosts.id).where(
                InstagramPosts.profile_username == values['profile_username'],
                InstagramPosts.post_shortcode == values['post_shortcode'])).scalar_one()
        session.commit()
    return parent_id, inserted
