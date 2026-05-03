"""
Classify Instagram posts (event vs not) and extract structured fields via Google Gemini.

By default reads posts from PostgreSQL (instagram_posts) and writes AI columns back to the same
rows. Candidate posts are those from the last AI_ANALYZE_RECENT_DAYS calendar days (UTC), set via
environment variable AI_ANALYZE_RECENT_DAYS (default 50), and by default excluding rows already
marked ai_analyzed. Pass --reanalyze to include already-analyzed rows in the window. Use
--use-temp-download for the legacy temp_download/<profile>/posts.json → posts_ai.json flow.

Environment (set in .env in project root, or .env.gemini):
  GEMINI_API_KEY or GOOGLE_API_KEY — required for the Gemini API.
  GEMINI_MODEL — optional; default gemini-2.5-flash (stable; 2.0 Flash is deprecated).
  GEMINI_MODEL_FALLBACK — optional comma-separated model ids tried after GEMINI_MODEL when
    429/free-tier quota is exhausted (default: see DEFAULT_MODEL_FALLBACK_CHAIN in gemini_analysis).
  GEMINI_DEFAULT_TZ — optional IANA zone for interpreting relative dates (default America/Los_Angeles).
  AI_ANALYZE_RECENT_DAYS — optional; max age in days for DB candidate posts (default 50).
  DB_* — see Blueprint_db / docker-compose for Postgres (required for default DB mode).

.env format: simple KEY=value lines (optional "export " prefix, # comments). A large
python-dotenv-incompatible .env is parsed line-by-line here so you do not get hundreds
of parse warnings. Put Gemini keys in .env.gemini if you prefer a tiny file.

Usage:
  python -m app.ai_analyze_service.ai_analyze --limit 3
    # DB: all profiles with pending posts in the recent window
  python -m app.ai_analyze_service.ai_analyze --profile seventhcollegestudentcouncil --limit 3
  python -m app.ai_analyze_service.ai_analyze --reanalyze
    # DB: also process rows with ai_analyzed true (within the recent window)
  python -m app.ai_analyze_service.ai_analyze --use-temp-download --profile foo  # legacy JSON

Model fallback chain (rate/quota):
  We start with GEMINI_MODEL. Each model gets up to several HTTP 429 retries; when that model’s
  retry budget is exhausted, we pass down to the next id in the chain (GEMINI_MODEL_FALLBACK or
  DEFAULT_MODEL_FALLBACK_CHAIN), in order, until one succeeds or every model is exhausted — then we
  stop with "We ran out of all models." Invalid-JSON repair attempts stay on the same model and do
  not advance the chain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

from google import genai

# Repo root (…/playwright-Playground); file is app/ai_analyze_service/ai_analyze.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEMP_DOWNLOAD = PROJECT_ROOT / "temp_download"


def _parse_env_file(path: Path) -> None:
    """Merge KEY=value lines into os.environ. Skips invalid lines silently (no python-dotenv)."""
    import os

    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val


def _load_env() -> None:
    _parse_env_file(PROJECT_ROOT / ".env")
    _parse_env_file(PROJECT_ROOT / ".env.gemini")


_load_env()

from Blueprint_db import InstagramPosts, SessionLocal  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.ai_analyze_service.candidates import (  # noqa: E402
    distinct_profile_usernames_recent,
    fetch_instagram_rows_for_profile,
)
from app.ai_analyze_service.config import ai_analyze_recent_days  # noqa: E402
from app.ai_analyze_service.gemini_analysis import (  # noqa: E402
    ai_to_db_update_values,
    analyze_post,
    default_tz,
    format_ai_terminal_summary,
    format_analysis_error,
    gemini_api_key,
    gemini_model_chain,
    primary_image_url_from_post,
)


def build_analyzer_post_dict(row: InstagramPosts) -> dict[str, Any]:
    """Map an instagram_posts row to the dict shape expected by Gemini prompts / image URLs."""
    comments: list[Any] = []
    cj = row.comments_json
    if isinstance(cj, dict):
        raw = cj.get("comments")
        if isinstance(raw, list):
            comments = raw
    omu: dict[str, Any] | list[Any] | None = row.additional_image_urls
    if not isinstance(omu, dict):
        omu = {}
    taken: int | None = None
    if row.posted_unix_seconds is not None:
        try:
            taken = int(row.posted_unix_seconds)
        except (TypeError, ValueError):
            taken = None
    return {
        "shortcode": row.post_shortcode,
        "permalink": row.post_url,
        "title": row.post_title or "",
        "taken_at": taken,
        "from_username": row.profile_username,
        "comments": comments,
        "openable_media_urls": omu,
    }


def _discover_profiles(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "posts.json").is_file():
            out.append(child)
    return out


def _load_existing_ai_by_shortcode(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    m: dict[str, dict[str, Any]] = {}
    for p in data.get("posts") or []:
        sc = p.get("shortcode")
        if isinstance(sc, str) and "ai" in p and isinstance(p["ai"], dict):
            m[sc] = p["ai"]
    return m


def process_profile_db(
    profile_username: str,
    *,
    client: genai.Client,
    model_chain: list[str],
    tz: ZoneInfo,
    limit: int | None,
    only_not_analyzed: bool,
    sleep_s: float,
    chain_start_idx: int = 0,
) -> int:
    """Load candidate rows from instagram_posts, run Gemini, UPDATE AI columns. Skips DB writes on errors."""
    pn = profile_username.lstrip("@").strip()
    rows = fetch_instagram_rows_for_profile(pn, only_not_analyzed=only_not_analyzed)
    if not rows:
        parts = [
            f"No rows for profile {pn!r}",
            f"posted within the last {ai_analyze_recent_days()} days",
        ]
        if only_not_analyzed:
            parts.append("with ai_analyzed = false")
        print(" ".join(parts), file=sys.stderr)
        return chain_start_idx

    analyzed = 0
    chain_offset = chain_start_idx

    for row in rows:
        if limit is not None and analyzed >= limit:
            break
        post = build_analyzer_post_dict(row)
        sc = post.get("shortcode")
        try:
            ai, used_idx = analyze_post(
                client, model_chain, post, tz, start_idx=chain_offset
            )
            chain_offset = used_idx
            vals = ai_to_db_update_values(
                ai,
                tz,
                fallback_main_image_url=primary_image_url_from_post(post),
            )
            with SessionLocal() as session:
                obj = session.execute(
                    select(InstagramPosts).where(
                        InstagramPosts.profile_username == pn,
                        InstagramPosts.post_shortcode == row.post_shortcode,
                    )
                ).scalar_one()
                for attr, value in vals.items():
                    setattr(obj, attr, value)
                obj.updated_at = datetime.now(timezone.utc)
                session.commit()
            analyzed += 1
            sc_str = sc if isinstance(sc, str) else "?"
            print(format_ai_terminal_summary(sc_str, ai))
            if sleep_s > 0:
                time.sleep(sleep_s)
        except Exception as e:
            brief = format_analysis_error(e)
            print(f"  Error {sc}: {brief}", file=sys.stderr)
            if sleep_s > 0:
                time.sleep(sleep_s)

    print(f"DB profile {pn}: updated {analyzed} post(s)")
    return chain_offset


def process_profile_dir(
    clean_dir: Path,
    *,
    client: genai.Client,
    model_chain: list[str],
    tz: ZoneInfo,
    limit: int | None,
    resume: bool,
    sleep_s: float,
    chain_start_idx: int = 0,
) -> int:
    posts_path = clean_dir / "posts.json"
    out_path = clean_dir / "posts_ai.json"
    bundle = json.loads(posts_path.read_text(encoding="utf-8"))
    posts = bundle.get("posts")
    if not isinstance(posts, list):
        print(f"Skip {clean_dir.name}: invalid posts.json", file=sys.stderr)
        return chain_start_idx

    existing_ai = _load_existing_ai_by_shortcode(out_path) if resume else {}
    analyzed = 0
    out_posts: list[dict[str, Any]] = []
    chain_offset = chain_start_idx

    for post in posts:
        if not isinstance(post, dict):
            continue
        sc = post.get("shortcode")
        if resume and isinstance(sc, str) and sc in existing_ai:
            merged = dict(post)
            merged["ai"] = existing_ai[sc]
            out_posts.append(merged)
            continue
        if limit is not None and analyzed >= limit:
            out_posts.append(dict(post))
            continue
        try:
            ai, used_idx = analyze_post(
                client, model_chain, post, tz, start_idx=chain_offset
            )
            chain_offset = used_idx
            row = dict(post)
            row["ai"] = ai.model_dump(mode="json", exclude_none=False)
            out_posts.append(row)
            analyzed += 1
            sc_str = sc if isinstance(sc, str) else "?"
            print(format_ai_terminal_summary(sc_str, ai))
            if sleep_s > 0:
                time.sleep(sleep_s)
        except Exception as e:
            brief = format_analysis_error(e)
            print(f"  Error {sc}: {brief}", file=sys.stderr)
            row = dict(post)
            row["ai"] = {
                "error": brief,
                "is_event": False,
                "description": f"Analysis failed: {brief}",
                "confidence": "low",
            }
            out_posts.append(row)
            analyzed += 1
            if sleep_s > 0:
                time.sleep(sleep_s)

    out_bundle = {
        "scraped_profile": bundle.get("scraped_profile", clean_dir.name),
        "posts": out_posts,
    }
    out_path.write_text(
        json.dumps(out_bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)} ({len(out_posts)} posts, analyzed {analyzed})")
    return chain_offset


def main(argv: list[str] | None = None) -> None:
    days = ai_analyze_recent_days()
    parser = argparse.ArgumentParser(
        description=(
            "Analyze Instagram posts with Gemini (default: PostgreSQL instagram_posts). "
            f"DB mode only loads posts from roughly the last {days} days (UTC), excluding "
            "ai_analyzed rows unless --reanalyze. If --profile is omitted, every profile with at "
            "least one such candidate row is processed."
        )
    )
    parser.add_argument(
        "--profile",
        help=(
            "Profile username. DB mode: optional; if omitted, all profiles with candidate rows are "
            "processed. Legacy: temp_download/<profile>/ (omit to scan all dirs with posts.json). "
            f"DB mode only loads posts from roughly the last {days} days."
        ),
    )
    parser.add_argument(
        "--use-temp-download",
        action="store_true",
        help="Read posts.json / write posts_ai.json under temp_download/ instead of the database",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Analyze at most this many posts per profile (DB: rows after ordering; legacy: others copied as-is)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Legacy JSON only: reuse posts_ai.json ai blocks by shortcode. (DB mode: default already skips analyzed.)",
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="DB: include rows where ai_analyzed is true (still within the recent-days window)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=3.0,
        help="Seconds to sleep between Gemini calls (default 3.0)",
    )
    args = parser.parse_args(argv)

    key = gemini_api_key()
    model_chain = gemini_model_chain()
    tz = default_tz()
    client = genai.Client(api_key=key)

    print(
        f"Gemini model chain ({len(model_chain)}): {' → '.join(model_chain)}",
        file=sys.stderr,
    )

    if args.use_temp_download:
        if args.profile:
            dirs = [TEMP_DOWNLOAD / args.profile.strip().lstrip("@")]
            if not (dirs[0] / "posts.json").is_file():
                print(f"No posts.json at {dirs[0]}", file=sys.stderr)
                sys.exit(1)
        else:
            dirs = _discover_profiles(TEMP_DOWNLOAD)

        if not dirs:
            print(f"No profiles with posts.json under {TEMP_DOWNLOAD}", file=sys.stderr)
            sys.exit(1)

        chain_offset = 0
        for d in dirs:
            print(f"Profile: {d.name}")
            chain_offset = process_profile_dir(
                d,
                client=client,
                model_chain=model_chain,
                tz=tz,
                limit=args.limit,
                resume=args.resume,
                sleep_s=args.sleep,
                chain_start_idx=chain_offset,
            )
        return

    only_not_analyzed = not args.reanalyze
    if args.profile and args.profile.strip():
        profile_list = [args.profile.strip().lstrip("@")]
    else:
        profile_list = distinct_profile_usernames_recent(only_not_analyzed=only_not_analyzed)
        if not profile_list:
            print(
                "DB mode: no profiles found with candidate posts in the recent window "
                f"(last {days} days"
                + (", pending analysis only" if only_not_analyzed else "")
                + "). "
                "Scrape first, widen AI_ANALYZE_RECENT_DAYS, pass --profile, or use --reanalyze.",
                file=sys.stderr,
            )
            sys.exit(1)

    chain_offset = 0
    for pn in profile_list:
        print(f"Profile: {pn}")
        chain_offset = process_profile_db(
            pn,
            client=client,
            model_chain=model_chain,
            tz=tz,
            limit=args.limit,
            only_not_analyzed=only_not_analyzed,
            sleep_s=args.sleep,
            chain_start_idx=chain_offset,
        )


if __name__ == "__main__":
    main(None)
