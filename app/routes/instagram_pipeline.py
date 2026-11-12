"""Run extraction/backfill, then optional isolated cover and Event Master sync stages."""

from __future__ import annotations

import os
import sys
import time
from app.instagram_scraper.recency import scrape_cutoff, run_cutoff
from app.pipeline_recovery.service import RecoveryRun, run_recovery

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from fastapi import Depends, APIRouter, HTTPException

from app.add_our_own_s3_url.add_our_own_s3_url import main as run_add_our_own_s3_url
from app.ai_analyze_service.ai_analyze import main as ai_analyze_main
from app.instagram_scraper.sync_ins import main as run_instagram_scraper
from app.event_cover_service.pipeline_stage import run_optional_cover_stage
from app.event_master_sync.sync_service import run_optional_event_master_sync

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
from app.service_auth import require_sync_service, SafeSyncRoute

router = APIRouter(route_class=SafeSyncRoute, dependencies=[Depends(require_sync_service)])


def _run_step(label: str, main_fn: Callable):
    """Safe lifecycle messages go both to server console and pipeline log."""
    started = time.monotonic()
    def log(status):
        message = f"pipeline stage={label} status={status} elapsed_seconds={time.monotonic() - started:.3f}"
        print(message, flush=True)
        if sys.__stdout__ is not None and sys.stdout is not sys.__stdout__:
            print(message, file=sys.__stdout__, flush=True)
    log("started")
    try:
        try:
            result = main_fn()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise HTTPException(status_code=500, detail={"message": f"Step failed: {label}"}) from None
            result = None
        log("failed" if isinstance(result, dict) and
            (result.get("status") in ("error", "partial_failure") or result.get("failed", 0)) else "completed")
        return result
    except BaseException:
        log("failed")
        raise


def _gemini_max_attempts() -> int | None:
    raw = (os.getenv("INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS") or "").strip()
    if not raw:
        return None
    try:
        cap = int(raw)
        if cap < 0:
            raise ValueError()
        return cap
    except ValueError:
        raise HTTPException(
            status_code=500,
            detail="INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS must be a nonnegative integer or blank",
        ) from None


@router.post("/instagram_pipeline")
def post_instagram_pipeline() -> dict[str, object]:
    """Abort on core-step errors; report optional cover/import errors separately."""
    cutoff = scrape_cutoff()
    recovery = RecoveryRun.configured(cutoff)
    cap = _gemini_max_attempts()
    analysis_options = {"max_attempts": cap} if cap is not None else {}
    committed_parent_ids: set[int] = set()
    analysis = dict(attempted=0, committed=0, failed=0)
    def analyze():
        ai_analyze_main([], committed_parent_ids=committed_parent_ids,
                        analysis_stats=analysis, **analysis_options)
        analysis['status'] = ('partial_failure' if analysis['committed'] else 'failed') if analysis['failed'] else (
            'completed' if analysis['committed'] else 'no-op')
        return analysis
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = PROJECT_ROOT / f"logs/Post_Instagram_Pipeline_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / "pipeline.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        with redirect_stdout(log_file), redirect_stderr(log_file), run_cutoff(cutoff), run_recovery(recovery):
            _run_step("run_instagram_scraper", run_instagram_scraper)
            _run_step("ai_analyze", analyze)
            _run_step("add_our_own_s3_url", lambda: run_add_our_own_s3_url([]))
            cover_stage = _run_step("generated_covers", lambda: run_optional_cover_stage(committed_parent_ids))
            cover_recovery = _run_step("cover_recovery", lambda: recovery.recover('cover'))
            event_master_sync = _run_step("event_master_sync", lambda: run_optional_event_master_sync(committed_parent_ids))
            event_master_recovery = _run_step("event_master_recovery", lambda: recovery.recover('event_master'))
    return {
        "success": True,
        "cover_recovery": cover_recovery,
        "event_master_recovery": event_master_recovery,
        "recovery_tracking_unavailable": recovery.tracking_unavailable,
        "steps_completed": ["sync_ins", "ai_analyze", "add_our_own_s3_url"],
        "log_file": str(log_path),
        "analysis": analysis,
        "cover_stage": cover_stage,
        "event_master_sync": event_master_sync,
    }
