"""FOMO HTTPS sync routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.fomo_sync.sync_service import fomo_sync_settings, run_full_fomo_sync


class FomoSyncResponse(BaseModel):
    success: bool
    rows_sent: int = Field(description="instagram_posts rows included in the sync attempt")
    batches: int = Field(description="Number of HTTP batches sent (0 if nothing to send)")
    error: str | None = None
    failed_batch: int | None = Field(
        default=None,
        description="Zero-based batch index when FOMO rejected or network failed",
    )
    http_status: int | None = Field(
        default=None,
        description="Last HTTP status from FOMO when available",
    )
    fomo_ingest_url: str | None = Field(
        default=None,
        description="Resolved ingest URL (after path normalization) used for this run",
    )


router = APIRouter()


@router.post("/fomo", response_model=FomoSyncResponse)
def post_sync_fomo() -> FomoSyncResponse:
    """Read all instagram_posts, POST JSON batches to FOMO_SYNC_URL, return aggregate result."""
    ingest_url, _, _, _ = fomo_sync_settings()
    r = run_full_fomo_sync()
    return FomoSyncResponse(
        success=r.success,
        rows_sent=r.rows_sent,
        batches=r.batches,
        error=r.error,
        failed_batch=r.failed_batch,
        http_status=r.http_status,
        fomo_ingest_url=ingest_url or None,
    )
