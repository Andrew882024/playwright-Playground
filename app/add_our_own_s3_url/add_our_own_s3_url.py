"""
For instagram_posts rows where is_event is true, AI-analyzed, main_image_url set, and own S3 URL empty:
download the image, upload to the configured S3 bucket, save own_s3_url_for_main_image.

Requires in .env (see Blueprint_db / project root):
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, S3_BUCKET

Usage:
  python -m app.add_our_own_s3_url.add_our_own_s3_url --limit 10
  python -m app.add_our_own_s3_url.add_our_own_s3_url --profile someuser --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import certifi

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

import boto3
from sqlalchemy import func, or_, select

from Blueprint_db import InstagramPosts, SessionLocal

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_MAX_BYTES = 18 * 1024 * 1024

_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _mime_from_url(url: str) -> str:
    base = url.lower().split("?", 1)[0]
    if base.endswith(".png"):
        return "image/png"
    if base.endswith(".webp"):
        return "image/webp"
    if base.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _fetch_image(url: str) -> tuple[bytes, str] | None:
    # macOS/Framework Python often lacks a usable default CA store for CDN TLS;
    # certifi supplies Mozilla's CA bundle so Instagram CDN URLs verify.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.instagram.com/",
        },
    )
    try:
        with urlopen(req, timeout=60, context=ssl_ctx) as resp:
            data = resp.read()
            ctype = resp.headers.get_content_type()
            if ctype and ctype.startswith("image/"):
                mime = ctype
            else:
                mime = _mime_from_url(url)
            if len(data) > _MAX_BYTES:
                return None
            return data, mime
    except (HTTPError, URLError, TimeoutError, OSError):
        return None


def _safe_segment(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", s)[:200] or "unknown"


def _extension_for_mime(mime: str) -> str:
    return _MIME_EXT.get(mime.split(";", 1)[0].strip().lower(), ".jpg")


def _public_object_url(bucket: str, region: str, key: str) -> str:
    encoded = quote(key, safe="/")
    return f"https://{bucket}.s3.{region}.amazonaws.com/{encoded}"


def _s3_client():
    region = (os.getenv("AWS_REGION") or "us-east-1").strip()
    ak = (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    sk = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    if not ak or not sk:
        raise RuntimeError("Missing AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY in environment")
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
    )


def rows_to_backfill(profile: str | None, limit: int | None) -> list[InstagramPosts]:
    trimmed_main = func.nullif(func.trim(InstagramPosts.main_image_url), "")
    trimmed_own = func.nullif(func.trim(InstagramPosts.own_s3_url_for_main_image), "")
    stmt = (
        select(InstagramPosts)
        .where(
            InstagramPosts.is_event.is_(True),
            trimmed_main.isnot(None),
            InstagramPosts.ai_analyzed.is_(True),
            or_(
                InstagramPosts.own_s3_url_for_main_image.is_(None),
                trimmed_own.is_(None),
            ),
        )
        .order_by(InstagramPosts.id.asc())
    )
    if profile is not None:
        pn = profile.lstrip("@").strip()
        stmt = stmt.where(InstagramPosts.profile_username == pn)
    if limit is not None:
        stmt = stmt.limit(limit)
    with SessionLocal() as session:
        return list(session.scalars(stmt).all())


def run_backfill(*, profile: str | None, limit: int | None, dry_run: bool) -> int:
    bucket = (os.getenv("S3_BUCKET") or "").strip()
    region = (os.getenv("AWS_REGION") or "us-east-1").strip()
    if not bucket and not dry_run:
        raise RuntimeError("Missing S3_BUCKET in environment")

    rows = rows_to_backfill(profile, limit)
    if not rows:
        print("No matching rows (is_event, main_image_url set, ai_analyzed, own_s3_url empty).")
        return 0

    client = None if dry_run else _s3_client()
    ok = 0
    for row in rows:
        url = (row.main_image_url or "").strip()
        if not url:
            continue
        if dry_run:
            print(f"[dry-run] id={row.id} shortcode={row.post_shortcode} would upload from {url[:80]}...")
            ok += 1
            continue
        fetched = _fetch_image(url)
        if not fetched:
            print(f"[skip] id={row.id} shortcode={row.post_shortcode} download failed")
            continue
        body, mime = fetched
        ext = _extension_for_mime(mime)
        key = (
            f"instagram_main_images/{_safe_segment(row.profile_username)}"
            f"/{row.post_shortcode}{ext}"
        )
        assert client is not None
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=mime.split(";", 1)[0].strip(),
            )
        except Exception as e:  # noqa: BLE001 — surface any S3/API error
            print(f"[skip] id={row.id} shortcode={row.post_shortcode} S3 error: {e}")
            continue

        public_url = _public_object_url(bucket, region, key)
        with SessionLocal() as session:
            db_row = session.get(InstagramPosts, row.id)
            if db_row is None:
                continue
            db_row.own_s3_url_for_main_image = public_url
            session.commit()
        print(f"[ok] id={row.id} shortcode={row.post_shortcode} -> {public_url}")
        ok += 1
    return ok


def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill own_s3_url_for_main_image via S3 upload (is_event rows only).",
    )
    p.add_argument("--profile", type=str, default=None, help="Only this instagram_posts.profile_username")
    p.add_argument("--limit", type=int, default=None, help="Max rows to process")
    p.add_argument("--dry-run", action="store_true", help="List rows only; no download/S3/DB writes")
    args = p.parse_args()
    n = run_backfill(profile=args.profile, limit=args.limit, dry_run=args.dry_run)
    print(f"Done. Processed successfully: {n}")


if __name__ == "__main__":
    main()
