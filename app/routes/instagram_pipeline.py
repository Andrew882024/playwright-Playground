"""Run Instagram scrape → AI analyze → S3 URL backfill in order; stop on first failure."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.add_our_own_s3_url.add_our_own_s3_url import main as run_add_our_own_s3_url
from app.ai_analyze_service.ai_analyze import main as run_ai_analyze
from app.instagram_scraper.sync_ins import main as run_instagram_scraper

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
router = APIRouter()


def _run_step(label: str, main_fn: Callable[[], None]) -> None:
    """Call main_fn; map SystemExit to HTTP 500 so the app process is not killed."""
    try:
        try:
            main_fn()
        except SystemExit as e:
            code = e.code
            if code not in (None, 0):
                raise HTTPException(
                    status_code=500,
                    detail={"message": f"Step failed: {label}", "exit_code": code},
                ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"message": f"Step failed: {label}", "error": str(e)},
        ) from e


@router.post("/instagram_pipeline")
def post_instagram_pipeline() -> dict[str, object]:
    """Run sync_ins, ai_analyze, and add_our_own_s3_url mains in order; abort on error."""
    # Import here so app startup does not load Gemini / Playwright / S3 stacks unless this route runs.
    

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = PROJECT_ROOT / f"logs/Post_Instagram_Pipeline_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / "pipeline.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        with redirect_stdout(log_file), redirect_stderr(log_file):
            _run_step("run_instagram_scraper", run_instagram_scraper)
            _run_step("ai_analyze", run_ai_analyze)
            _run_step("add_our_own_s3_url", run_add_our_own_s3_url)
    return {
        "success": True,
        "steps_completed": ["sync_ins", "ai_analyze", "add_our_own_s3_url"],
        "log_file": str(log_path),
    }
