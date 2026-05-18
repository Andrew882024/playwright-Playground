"""
Multithreaded Gemini analysis: Redis queues per model.

By default runs 4 worker threads in parallel (one per model queue). New jobs are
spread round-robin across all four queues so each worker has tasks, not only the
first model. Enqueues from DB, then workers process until all queues are empty.

Run from repo root (Redis must be reachable; set REDIS_URL or REDIS_HOST/PORT):
  python -m app.ai_analyze_service_mutithread.ai_analyze_mt
  python -m app.ai_analyze_service_mutithread.ai_analyze_mt --enqueue-only
  python -m app.ai_analyze_service_mutithread.ai_analyze_mt --workers-only
  python -m app.ai_analyze_service_mutithread.ai_analyze_mt --profile foo --limit 10

Imports only ``Blueprint_db`` (repo root) and this package — not other ``app.*`` modules.
See contract/contract.md.
"""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from sqlalchemy import select
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _parse_env_file(path: Path) -> None:
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
    _parse_env_file(_REPO_ROOT / ".env")
    _parse_env_file(_REPO_ROOT / ".env.gemini")


_load_env()

from Blueprint_db import InstagramPosts, SessionLocal  # noqa: E402

from app.ai_analyze_service_mutithread.candidates import (  # noqa: E402
    fetch_all_candidate_post_keys,
    fetch_instagram_row,
)
from app.ai_analyze_service_mutithread.config import ai_analyze_recent_days  # noqa: E402
from app.ai_analyze_service_mutithread.gemini_analysis import (  # noqa: E402
    ALL_MODELS_EXHAUSTED_MSG,
    ModelQuotaExhausted,
    ai_to_db_update_values,
    analyze_post_single_model,
    default_tz,
    format_ai_terminal_summary,
    format_analysis_error,
    gemini_api_key,
    primary_image_url_from_post,
)
from app.ai_analyze_service_mutithread.redis_queue import (  # noqa: E402
    DEFAULT_WORKER_COUNT,
    MODEL_QUEUE_CHAIN,
    AnalyzeJob,
    ack_job,
    claim_job,
    enqueue_job,
    flush_all_queues,
    initial_model_for_job_index,
    queue_depths,
    reassign_job,
)
from app.ai_analyze_service_mutithread.redis_queue import redis_client  # noqa: E402


def build_analyzer_post_dict(row: InstagramPosts) -> dict[str, Any]:
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


def enqueue_candidates_from_db(
    r,
    *,
    only_not_analyzed: bool,
    profile_username: str | None,
    limit: int | None,
    flush_queues: bool,
) -> int:
    if flush_queues:
        flush_all_queues(r)
    keys = fetch_all_candidate_post_keys(
        only_not_analyzed=only_not_analyzed,
        profile_username=profile_username,
    )
    if limit is not None:
        keys = keys[:limit]
    n = 0
    for i, (prof, sc) in enumerate(keys):
        model = initial_model_for_job_index(i)
        enqueue_job(r, model, AnalyzeJob(profile_username=prof, post_shortcode=sc))
        n += 1
    return n


def _commit_analysis(
    profile_username: str,
    post_shortcode: str,
    post: dict[str, Any],
    ai,
    tz: ZoneInfo,
) -> None:
    pn = profile_username.lstrip("@").strip()
    vals = ai_to_db_update_values(
        ai,
        tz,
        fallback_main_image_url=primary_image_url_from_post(post),
    )
    with SessionLocal() as session:
        obj = session.execute(
            select(InstagramPosts).where(
                InstagramPosts.profile_username == pn,
                InstagramPosts.post_shortcode == post_shortcode,
            )
        ).scalar_one()
        for attr, value in vals.items():
            setattr(obj, attr, value)
        obj.updated_at = datetime.now(timezone.utc)
        session.commit()


def process_one_job(
    *,
    client: genai.Client,
    model: str,
    job: AnalyzeJob,
    tz: ZoneInfo,
) -> None:
    row = fetch_instagram_row(job.profile_username, job.post_shortcode)
    if row is None:
        print(
            f"  Skip missing row {job.profile_username}/{job.post_shortcode}",
            file=sys.stderr,
        )
        return
    if row.ai_analyzed:
        print(
            f"  Skip already analyzed {job.profile_username}/{job.post_shortcode}",
            file=sys.stderr,
        )
        return
    post = build_analyzer_post_dict(row)
    sc = post.get("shortcode")
    ai = analyze_post_single_model(client, model, post, tz)
    _commit_analysis(job.profile_username, job.post_shortcode, post, ai, tz)
    sc_str = sc if isinstance(sc, str) else "?"
    print(format_ai_terminal_summary(sc_str, ai))


def _worker_loop(
    model: str,
    *,
    client: genai.Client,
    tz: ZoneInfo,
    stop_event: threading.Event,
    brpop_timeout: int,
) -> None:
    r = redis_client()
    while not stop_event.is_set():
        claimed = claim_job(r, model, timeout_s=brpop_timeout)
        if claimed is None:
            depths = queue_depths(r)
            if all(d[0] == 0 and d[1] == 0 for d in depths.values()):
                break
            continue
        raw, job = claimed
        try:
            # Contract: commit DB in process_one_job, then ACK (consume) the Redis message.
            process_one_job(client=client, model=model, job=job, tz=tz)
            ack_job(r, model, raw)
        except ModelQuotaExhausted:
            if not reassign_job(r, model, raw, job):
                print(
                    f"  {job.profile_username}/{job.post_shortcode}: "
                    f"{ALL_MODELS_EXHAUSTED_MSG}",
                    file=sys.stderr,
                )
        except Exception as e:
            ack_job(r, model, raw)
            brief = format_analysis_error(e)
            print(
                f"  Error {job.profile_username}/{job.post_shortcode} "
                f"[{model}]: {brief}",
                file=sys.stderr,
            )


def run_workers(
    *,
    client: genai.Client,
    tz: ZoneInfo,
    brpop_timeout: int = 5,
) -> None:
    stop = threading.Event()
    threads: list[threading.Thread] = []
    for model in MODEL_QUEUE_CHAIN:
        t = threading.Thread(
            target=_worker_loop,
            name=f"worker-{model}",
            kwargs={
                "model": model,
                "client": client,
                "tz": tz,
                "stop_event": stop,
                "brpop_timeout": brpop_timeout,
            },
            daemon=True,
        )
        t.start()
        threads.append(t)
        print(f"Started worker for {model}", file=sys.stderr)
    print(
        f"Running {len(threads)} workers (contract default: {DEFAULT_WORKER_COUNT}).",
        file=sys.stderr,
    )

    for t in threads:
        t.join()
    print("All workers finished (queues empty).", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    days = ai_analyze_recent_days()
    parser = argparse.ArgumentParser(
        description=(
            f"Multithreaded Gemini analysis via Redis. Default: {DEFAULT_WORKER_COUNT} "
            "workers (one thread per model queue). "
            f"DB candidates: posts from the last {days} days (UTC), "
            "ai_analyzed=false unless --reanalyze."
        )
    )
    parser.add_argument("--profile", help="Only enqueue/process this profile username")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max posts to enqueue from DB",
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="Include rows where ai_analyzed is true (still within recent window)",
    )
    parser.add_argument(
        "--enqueue-only",
        action="store_true",
        help="Load candidates from DB into Redis queues, then exit",
    )
    parser.add_argument(
        "--workers-only",
        action="store_true",
        help="Run worker threads only (queues must already be populated)",
    )
    parser.add_argument(
        "--flush-queues",
        action="store_true",
        help="Clear all model queues before enqueueing",
    )
    parser.add_argument(
        "--brpop-timeout",
        type=int,
        default=5,
        help="Seconds to block on BRPOP per iteration (default 5)",
    )
    args = parser.parse_args(argv)

    if args.enqueue_only and args.workers_only:
        parser.error("Use at most one of --enqueue-only and --workers-only")

    r = redis_client()
    try:
        r.ping()
    except Exception as e:
        print(
            f"Cannot connect to Redis ({e}). "
            "Start Redis and set REDIS_URL or REDIS_HOST/REDIS_PORT.",
            file=sys.stderr,
        )
        sys.exit(1)

    only_not_analyzed = not args.reanalyze
    profile = args.profile.strip().lstrip("@") if args.profile and args.profile.strip() else None

    if not args.workers_only:
        n = enqueue_candidates_from_db(
            r,
            only_not_analyzed=only_not_analyzed,
            profile_username=profile,
            limit=args.limit,
            flush_queues=args.flush_queues,
        )
        print(
            f"Enqueued {n} job(s) round-robin across {DEFAULT_WORKER_COUNT} model queues",
            file=sys.stderr,
        )
        depths = queue_depths(r)
        for m, (q, p) in depths.items():
            if q or p:
                print(f"  {m}: queue={q} processing={p}", file=sys.stderr)

    if args.enqueue_only:
        return

    key = gemini_api_key()
    tz = default_tz()
    client = genai.Client(api_key=key)
    print(
        f"Workers for models: {' → '.join(MODEL_QUEUE_CHAIN)}",
        file=sys.stderr,
    )
    run_workers(client=client, tz=tz, brpop_timeout=args.brpop_timeout)


if __name__ == "__main__":
    main(None)
