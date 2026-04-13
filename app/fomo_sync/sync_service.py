"""Serialize InstagramPosts rows and POST JSON batches to FOMO ingest URL."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select

from Blueprint_db import InstagramPosts, SessionLocal


def _dt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def instagram_post_row_to_dict(row: InstagramPosts) -> dict[str, Any]:
    """JSON-serializable dict matching instagram_posts columns (snake_case)."""
    return {
        "id": row.id,
        "profile_username": row.profile_username,
        "post_shortcode": row.post_shortcode,
        "post_url": row.post_url,
        "post_title": row.post_title,
        "posted_unix_seconds": row.posted_unix_seconds,
        "posted_time": _dt_iso(row.posted_time),
        "instagram_media_id": row.instagram_media_id,
        "caption": row.caption,
        "comments_json": row.comments_json,
        "main_image_url": row.main_image_url,
        "additional_image_urls": row.additional_image_urls,
        "is_event": row.is_event,
        "event_title": row.event_title,
        "provider_name": row.provider_name,
        "post_description": row.post_description,
        "location": row.location,
        "duration_in_minutes": row.duration_in_minutes,
        "confidence": row.confidence,
        "ai_model": row.ai_model,
        "ai_analyzed": row.ai_analyzed,
        "event_start_at": _dt_iso(row.event_start_at),
        "event_end_at": _dt_iso(row.event_end_at),
        "own_s3_url_for_main_image": row.own_s3_url_for_main_image,
        "created_at": _dt_iso(row.created_at),
        "updated_at": _dt_iso(row.updated_at),
    }


def count_instagram_posts() -> int:
    with SessionLocal() as session:
        n = session.scalar(select(func.count()).select_from(InstagramPosts))
        return int(n or 0)


def iter_instagram_post_orm_batches(
    batch_size: int,
) -> Iterator[tuple[int, list[InstagramPosts]]]:
    """Keyset pagination by id — bounded memory for large tables (e.g.10k+ rows)."""
    if batch_size <= 0:
        batch_size = 250
    last_id = 0
    batch_index = 0
    while True:
        with SessionLocal() as session:
            stmt = (
                select(InstagramPosts)
                .where(InstagramPosts.id > last_id)
                .order_by(InstagramPosts.id.asc())
                .limit(batch_size)
            )
            rows = list(session.scalars(stmt).all())
        if not rows:
            break
        last_id = rows[-1].id
        yield batch_index, rows
        batch_index += 1


def _chunks_from_list(
    items: list[dict[str, Any]], size: int
) -> Iterator[tuple[int, list[dict[str, Any]], int]]:
    n = len(items)
    if size <= 0:
        size = 250
    batch_count = (n + size - 1) // size if n else 0
    for i in range(0, n, size):
        yield i // size, items[i : i + size], batch_count


@dataclass
class FomoPushResult:
    success: bool
    rows_sent: int
    batches: int
    error: str | None = None
    failed_batch: int | None = None
    http_status: int | None = None


def normalize_fomo_ingest_url(url: str) -> str:
    """Normalize FOMO ingest URL to match OpenAPI ``POST /sync/instagram-posts``.

    - Host-only URLs get path ``/sync/instagram-posts``.
    - Legacy ``/instagram-posts`` (no ``/sync``) is rewritten to ``/sync/instagram-posts``.
    - Otherwise the path is left as set (e.g. ``/api/sync/instagram-posts`` behind a gateway).
    """
    url = (url or "").strip()
    if not url:
        return url
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/") or ""
    if not path:
        parsed = parsed._replace(path="/sync/instagram-posts")
        return parsed.geturl()
    if path == "/instagram-posts":
        parsed = parsed._replace(path="/sync/instagram-posts")
        return parsed.geturl()
    return url


def _fomo_request_headers(api_key: str) -> dict[str, str]:
    """Match ``verify_fomo_sync_key`` on the receiver: Bearer vs ``X-API-Key`` vs raw ``Authorization``."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if not api_key:
        return headers
    mode = (os.getenv("FOMO_SYNC_AUTH_MODE") or "bearer").strip().lower()
    if mode in ("x-api-key", "x_api_key", "apikey"):
        headers["X-API-Key"] = api_key
    elif mode in ("raw", "authorization_raw"):
        headers["Authorization"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def fomo_sync_settings() -> tuple[str, str, int, float]:
    url = normalize_fomo_ingest_url((os.getenv("FOMO_SYNC_URL") or "").strip())
    key = (os.getenv("FOMO_SYNC_API_KEY") or "").strip()
    try:
        batch = int((os.getenv("FOMO_SYNC_BATCH_SIZE") or "250").strip())
    except ValueError:
        batch = 250
    try:
        timeout = float((os.getenv("FOMO_SYNC_TIMEOUT_SECONDS") or "300").strip())
    except ValueError:
        timeout = 300.0
    return url, key, batch, timeout


def _post_one_http_batch(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    batch_index: int,
    batch_count: int,
    rows: list[dict[str, Any]],
    total_rows: int,
) -> FomoPushResult | None:
    """Return FomoPushResult on failure, None if FOMO accepted the batch."""
    payload = {
        "batch_index": batch_index,
        "batch_count": batch_count,
        "rows": rows,
    }
    try:
        resp = client.post(url, headers=headers, json=payload)
    except httpx.RequestError as e:
        return FomoPushResult(
            success=False,
            rows_sent=total_rows,
            batches=batch_count,
            error=f"HTTP request failed (batch {batch_index}): {e}",
            failed_batch=batch_index,
        )

    if resp.status_code != 200:
        hint = ""
        if resp.status_code == 404:
            hint = (
                " (path likely wrong: FOMO ingest is POST /sync/instagram-posts, e.g. "
                "FOMO_SYNC_URL=http://localhost:8002/sync/instagram-posts)"
            )
        elif resp.status_code in (401, 403):
            hint = (
                " (auth rejected: try FOMO_SYNC_AUTH_MODE=x_api_key if the server uses X-API-Key, "
                "or authorization_raw if it expects the raw secret in Authorization)"
            )
        elif resp.status_code == 422:
            hint = (
                " (body validation failed: ensure InstagramIngestBatch fields match "
                "batch_index, batch_count, rows as sent by this client)"
            )
        detail = resp.text[:800]
        try:
            err_json = resp.json()
            if isinstance(err_json, dict) and "detail" in err_json:
                detail = f"{err_json.get('detail')!r}"[:800]
        except json.JSONDecodeError:
            pass
        return FomoPushResult(
            success=False,
            rows_sent=total_rows,
            batches=batch_count,
            error=f"FOMO returned HTTP {resp.status_code} (batch {batch_index}){hint}: {detail}",
            failed_batch=batch_index,
            http_status=resp.status_code,
        )

    try:
        body = resp.json()
    except json.JSONDecodeError:
        return FomoPushResult(
            success=False,
            rows_sent=total_rows,
            batches=batch_count,
            error=f"FOMO response was not JSON (batch {batch_index})",
            failed_batch=batch_index,
            http_status=resp.status_code,
        )

    if not isinstance(body, dict) or body.get("ok") is not True:
        err = body.get("error") if isinstance(body, dict) else None
        msg = err if isinstance(err, str) else str(body)[:500]
        return FomoPushResult(
            success=False,
            rows_sent=total_rows,
            batches=batch_count,
            error=f"FOMO reported failure (batch {batch_index}): {msg}",
            failed_batch=batch_index,
            http_status=resp.status_code,
        )
    return None


def push_instagram_posts_to_fomo(
    *,
    url: str,
    api_key: str,
    rows: list[dict[str, Any]],
    batch_size: int,
    timeout_seconds: float,
) -> FomoPushResult:
    """POST pre-built row dicts in batches (loads all into memory — prefer run_full_fomo_sync for huge tables)."""
    if not url:
        return FomoPushResult(
            success=False,
            rows_sent=0,
            batches=0,
            error="FOMO_SYNC_URL is not set",
        )

    n = len(rows)
    if n == 0:
        return FomoPushResult(success=True, rows_sent=0, batches=0)

    url = normalize_fomo_ingest_url(url)
    headers = _fomo_request_headers(api_key)

    timeout = httpx.Timeout(timeout_seconds, connect=min(30.0, timeout_seconds))
    batches_total = (n + batch_size - 1) // batch_size

    with httpx.Client(timeout=timeout) as client:
        for batch_index, chunk, _ in _chunks_from_list(rows, batch_size):
            err = _post_one_http_batch(
                client,
                url=url,
                headers=headers,
                batch_index=batch_index,
                batch_count=batches_total,
                rows=chunk,
                total_rows=n,
            )
            if err is not None:
                return err

    return FomoPushResult(success=True, rows_sent=n, batches=batches_total)


def run_full_fomo_sync() -> FomoPushResult:
    """Stream instagram_posts from DB in id-ordered batches; POST each batch without holding the full table."""
    url, key, batch_size, timeout_s = fomo_sync_settings()
    if not url:
        return FomoPushResult(
            success=False,
            rows_sent=0,
            batches=0,
            error="FOMO_SYNC_URL is not set",
        )

    total = count_instagram_posts()
    if total == 0:
        return FomoPushResult(success=True, rows_sent=0, batches=0)

    batch_count = (total + batch_size - 1) // batch_size
    headers = _fomo_request_headers(key)

    timeout = httpx.Timeout(timeout_s, connect=min(30.0, timeout_s))
    rows_sent = 0

    with httpx.Client(timeout=timeout) as client:
        for batch_index, orm_rows in iter_instagram_post_orm_batches(batch_size):
            chunk = [instagram_post_row_to_dict(r) for r in orm_rows]
            rows_sent += len(chunk)
            err = _post_one_http_batch(
                client,
                url=url,
                headers=headers,
                batch_index=batch_index,
                batch_count=batch_count,
                rows=chunk,
                total_rows=total,
            )
            if err is not None:
                return err

    return FomoPushResult(success=True, rows_sent=rows_sent, batches=batch_count)
