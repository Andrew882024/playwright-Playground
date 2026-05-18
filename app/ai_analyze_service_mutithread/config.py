"""AI analyze service configuration (env-tunable)."""

from __future__ import annotations

import os

_DEFAULT_AI_ANALYZE_RECENT_DAYS = 50


def ai_analyze_recent_days() -> int:
    """Max age in calendar days (UTC) for DB candidate posts; minimum 1."""
    raw = (os.environ.get("AI_ANALYZE_RECENT_DAYS") or "").strip()
    if not raw:
        return _DEFAULT_AI_ANALYZE_RECENT_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_AI_ANALYZE_RECENT_DAYS
