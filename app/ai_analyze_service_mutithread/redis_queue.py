"""Redis list queues: one queue + processing list per Gemini model."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

import redis

QUEUE_PREFIX = "ai_analyze_mt"

# Contract: four worker threads, one dedicated queue per model.
DEFAULT_WORKER_COUNT = 4

MODEL_QUEUE_CHAIN: tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

if len(MODEL_QUEUE_CHAIN) != DEFAULT_WORKER_COUNT:
    raise RuntimeError(
        f"MODEL_QUEUE_CHAIN must have {DEFAULT_WORKER_COUNT} models, "
        f"got {len(MODEL_QUEUE_CHAIN)}"
    )


@dataclass
class AnalyzeJob:
    profile_username: str
    post_shortcode: str
    attempted_models: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> AnalyzeJob:
        data: dict[str, Any] = json.loads(raw)
        attempted = data.get("attempted_models") or []
        if not isinstance(attempted, list):
            attempted = []
        return cls(
            profile_username=str(data["profile_username"]),
            post_shortcode=str(data["post_shortcode"]),
            attempted_models=[str(m) for m in attempted],
        )


def redis_client() -> redis.Redis:
    url = (os.environ.get("REDIS_URL") or "").strip()
    if url:
        return redis.Redis.from_url(url, decode_responses=True)
    host = (os.environ.get("REDIS_HOST") or "127.0.0.1").strip()
    port = int((os.environ.get("REDIS_PORT") or "6379").strip())
    db = int((os.environ.get("REDIS_DB") or "0").strip())
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def queue_key(model: str) -> str:
    return f"{QUEUE_PREFIX}:queue:{model}"


def processing_key(model: str) -> str:
    return f"{QUEUE_PREFIX}:processing:{model}"


def next_model_in_chain(model: str) -> str | None:
    try:
        idx = MODEL_QUEUE_CHAIN.index(model)
    except ValueError:
        return None
    if idx + 1 >= len(MODEL_QUEUE_CHAIN):
        return None
    return MODEL_QUEUE_CHAIN[idx + 1]


def first_model_in_chain() -> str:
    return MODEL_QUEUE_CHAIN[0]


def initial_model_for_job_index(index: int) -> str:
    """Round-robin: spread new jobs across all worker queues so four workers run in parallel."""
    return MODEL_QUEUE_CHAIN[index % len(MODEL_QUEUE_CHAIN)]


def enqueue_job(r: redis.Redis, model: str, job: AnalyzeJob) -> None:
    r.lpush(queue_key(model), job.to_json())


def claim_job(r: redis.Redis, model: str, timeout_s: int = 5) -> tuple[str, AnalyzeJob] | None:
    """BRPOPLPUSH from queue to processing; returns (raw_payload, job) or None on timeout."""
    raw = r.brpoplpush(queue_key(model), processing_key(model), timeout=timeout_s)
    if raw is None:
        return None
    return raw, AnalyzeJob.from_json(raw)


def ack_job(r: redis.Redis, model: str, raw_payload: str) -> None:
    r.lrem(processing_key(model), 1, raw_payload)


def reassign_job(r: redis.Redis, from_model: str, raw_payload: str, job: AnalyzeJob) -> bool:
    """Remove from processing list and push to the next model queue. Returns False if no next model."""
    r.lrem(processing_key(from_model), 1, raw_payload)
    nxt = next_model_in_chain(from_model)
    if nxt is None:
        return False
    if from_model not in job.attempted_models:
        job.attempted_models.append(from_model)
    enqueue_job(r, nxt, job)
    return True


def queue_depths(r: redis.Redis) -> dict[str, tuple[int, int]]:
    """Per model: (pending queue length, processing list length)."""
    out: dict[str, tuple[int, int]] = {}
    for m in MODEL_QUEUE_CHAIN:
        out[m] = (int(r.llen(queue_key(m))), int(r.llen(processing_key(m))))
    return out


def flush_all_queues(r: redis.Redis) -> None:
    for m in MODEL_QUEUE_CHAIN:
        r.delete(queue_key(m), processing_key(m))
