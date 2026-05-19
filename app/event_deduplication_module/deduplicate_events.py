"""Mark duplicate event posts on instagram_posts using Gemini (see contract/contract.md).

Run from repo root:
  python -m app.event_deduplication_module.deduplicate_events

Imports only ``Blueprint_db`` (project root), not other ``app.*`` packages.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")
load_dotenv(_REPO_ROOT / ".env.gemini")

from Blueprint_db import InstagramPosts, SessionLocal  # noqa: E402


DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"


def _gemini_api_key() -> str:
    k = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY in the environment.")
    return k


def _model_chain() -> list[str]:
    primary = (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    extra = (os.environ.get("GEMINI_MODEL_FALLBACK") or "").strip()
    chain = [primary]
    for part in extra.split(","):
        p = part.strip()
        if p and p not in chain:
            chain.append(p)
    return chain


def _client_error_blob(e: ClientError) -> str:
    d = e.details
    if isinstance(d, (dict, list)):
        try:
            return json.dumps(d, default=str)
        except (TypeError, ValueError):
            return str(d)
    return str(d or "")


def _generate_with_429_retry(
    client: genai.Client,
    model: str,
    contents: list[Any],
    config: types.GenerateContentConfig,
    *,
    max_quota_retries: int = 5,
) -> Any:
    for attempt in range(max_quota_retries):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except ClientError as e:
            if e.code != 429:
                raise
            blob = _client_error_blob(e)
            if "limit: 0" in blob and "free_tier" in blob.lower():
                raise RuntimeError(
                    "Gemini API: free-tier quota exhausted for this model (limit 0)."
                ) from e
            if attempt == max_quota_retries - 1:
                raise
            delay = 5.0
            print(
                f"Gemini model {model!r}: 429, waiting {delay:.1f}s "
                f"(attempt {attempt + 2}/{max_quota_retries})...",
                file=sys.stderr,
            )
            time.sleep(delay)


def _parse_json_text(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty model response")
    return json.loads(raw)


class DuplicateGroupAI(BaseModel):
    member_ids: list[int] = Field(description="Post ids that are the same real-world event.")
    canonical_id: int = Field(
        description="The single post id to treat as the non-duplicate (best source)."
    )


class DedupResponseAI(BaseModel):
    groups: list[DuplicateGroupAI] = Field(
        default_factory=list,
        description="Each group is duplicate copies of one event; size >= 2.",
    )


def _row_to_prompt_dict(row: InstagramPosts) -> dict[str, Any]:
    def iso(dt: Any) -> str | None:
        if dt is None:
            return None
        return dt.isoformat()

    return {
        "id": row.id,
        "profile_username": row.profile_username,
        "post_title": row.post_title,
        "posted_unix_seconds": row.posted_unix_seconds,
        "posted_time": iso(row.posted_time),
        "is_event": row.is_event,
        "event_title": row.event_title,
        "provider_name": row.provider_name,
        "post_description": row.post_description,
        "location": row.location,
        "duration_in_minutes": row.duration_in_minutes,
        "ai_analyzed": row.ai_analyzed,
        "event_start_at": iso(row.event_start_at),
    }


def _group_eligible_for_processing(rows: list[InstagramPosts]) -> bool:
    """Contract: same start time, 2+ posts, and at least one ``is_duplicated`` is null."""
    return any(r.is_duplicated is None for r in rows)


_SYSTEM = """You compare Instagram posts that share the same scheduled event start time.
Some are duplicate announcements of the same real-world event; others may be different events
that happen to start at the same time. There may be different small events within a larger event. 
These small events should not be identified as different events; we want the one that can best 
represent the larger event.

Return a single JSON object matching the schema. No markdown, no code fences.

Rules:
- Put posts that describe the SAME event (same title/host/location intent, reposts, reshares)
  in one group with member_ids (2+ ids) and pick exactly one canonical_id from member_ids
  (prefer clearer title, official host, or richer description).
- Posts that are clearly different events at the same clock time go in no group (omit them).
- Groups must be disjoint: each post id appears in at most one group.
- If two posts are not duplicates, do not put them in the same group.
"""


def _user_prompt(payload: list[dict[str, Any]]) -> str:
    return (
        "Same event_start_at bucket. Decide duplicate groups.\n\n"
        f"posts = {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        'Output JSON with key "groups": list of '
        '{"member_ids": int[], "canonical_id": int}. '
        "Only include groups with 2+ member_ids."
    )


def _call_gemini(
    client: genai.Client,
    model_chain: list[str],
    payload: list[dict[str, Any]],
) -> DedupResponseAI:
    contents = [_user_prompt(payload)]
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        temperature=0.1,
        response_mime_type="application/json",
    )
    last_err: Exception | None = None
    for model in model_chain:
        try:
            resp = _generate_with_429_retry(client, model, contents, config)
            raw = (resp.text or "").strip()
            data = _parse_json_text(raw)
            return DedupResponseAI.model_validate(data)
        except (ClientError, json.JSONDecodeError, ValidationError, ValueError) as e:
            last_err = e
            print(f"Gemini model {model!r} failed: {e}", file=sys.stderr)
            continue
    raise RuntimeError(f"All Gemini models failed. Last error: {last_err!r}") from last_err


def _apply_dedup_updates(
    rows: list[InstagramPosts],
    parsed: DedupResponseAI,
) -> dict[str, int] | None:
    """Return counts; mutates rows in session (caller commits).

    Posts in a valid duplicate group get ``is_duplicated=True`` except ``canonical_id`` (False).
    All other posts in the bucket get ``is_duplicated=False``.
    Returns None if the model output is inconsistent (overlapping groups).
    """
    id_set = {r.id for r in rows}
    seen_in_groups: set[int] = set()
    duplicate_true: set[int] = set()

    for g in parsed.groups:
        members = list(dict.fromkeys(g.member_ids))
        if len(members) < 2:
            continue
        if g.canonical_id not in members:
            print(
                f"Skip group: canonical_id {g.canonical_id} not in {members}",
                file=sys.stderr,
            )
            continue
        unknown = [m for m in members if m not in id_set]
        if unknown:
            print(f"Skip group: ids not in bucket {unknown}", file=sys.stderr)
            continue
        overlap = seen_in_groups & set(members)
        if overlap:
            print(
                f"Reject bucket: post ids in multiple duplicate groups {overlap}",
                file=sys.stderr,
            )
            return None
        seen_in_groups.update(members)
        for m in members:
            if m != g.canonical_id:
                duplicate_true.add(m)

    updated = 0
    for row in rows:
        want = row.id in duplicate_true
        if row.is_duplicated != want:
            row.is_duplicated = want
            updated += 1
    return {"rows_touched": updated, "bucket_size": len(rows)}


@dataclass
class RunStats:
    buckets_considered: int = 0
    buckets_processed: int = 0
    rows_updated: int = 0


def run_deduplication() -> RunStats:
    stats = RunStats()
    key = _gemini_api_key()
    chain = _model_chain()
    client = genai.Client(api_key=key)

    with SessionLocal() as session:
        times_stmt = (
            select(InstagramPosts.event_start_at)
            .where(
                InstagramPosts.is_event.is_(True),
                InstagramPosts.event_start_at.isnot(None),
            )
            .group_by(InstagramPosts.event_start_at)
            .having(func.count() >= 2)
        )
        start_times = list(session.scalars(times_stmt).all())

    for est in start_times:
        stats.buckets_considered += 1
        with SessionLocal() as session:
            stmt = (
                select(InstagramPosts)
                .where(
                    InstagramPosts.is_event.is_(True),
                    InstagramPosts.event_start_at == est,
                )
                .order_by(InstagramPosts.id.asc())
            )
            rows = list(session.scalars(stmt).all())
            if len(rows) < 2:
                continue
            if not _group_eligible_for_processing(rows):
                continue

            payload = [_row_to_prompt_dict(r) for r in rows]
            try:
                parsed = _call_gemini(client, chain, payload)
            except RuntimeError as e:
                print(f"Bucket {est}: {e}", file=sys.stderr)
                continue

            counts = _apply_dedup_updates(rows, parsed)
            if counts is None:
                session.rollback()
                continue
            session.commit()
            stats.buckets_processed += 1
            stats.rows_updated += counts["rows_touched"]
            print(
                f"Committed bucket event_start_at={est}: "
                f"updated {counts['rows_touched']} / {counts['bucket_size']} rows",
            )

    return stats


def main() -> None:
    stats = run_deduplication()
    print(
        f"Done. buckets_considered={stats.buckets_considered} "
        f"buckets_processed={stats.buckets_processed} rows_updated={stats.rows_updated}"
    )


if __name__ == "__main__":
    main()
