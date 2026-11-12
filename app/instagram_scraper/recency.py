"""Shared UTC run cutoff; unknown timestamps are not active posts."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

_RUN_CUTOFF = ContextVar('instagram_run_cutoff', default=None)


def scrape_cutoff(now=None):
    days = int(os.getenv('INSTAGRAM_SCRAPE_MAX_AGE_DAYS') or '40')
    if days <= 0:
        raise ValueError('INSTAGRAM_SCRAPE_MAX_AGE_DAYS must be a positive integer')
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def active_cutoff():
    return _RUN_CUTOFF.get() or scrape_cutoff()


@contextmanager
def run_cutoff(cutoff):
    token = _RUN_CUTOFF.set(cutoff)
    try:
        yield
    finally:
        _RUN_CUTOFF.reset(token)


def recent_post(post, cutoff):
    try:
        return datetime.fromtimestamp(int(post['taken_at']), timezone.utc) >= cutoff
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return False


class OldPostStopper:
    """Ignore initial grid (including unlabelled pins). Require two subsequent
    batches of >=3 distinct, explicitly unpinned, dated, descending old posts.
    Unknown pin status cannot establish chronological exhaustion.
    """
    def __init__(self, initial_codes, cutoff):
        self.seen = set(initial_codes)
        self.cutoff = cutoff
        self.old_batches = 0

    def observe(self, posts):
        new = [p for p in posts if p.get('shortcode') not in self.seen]
        self.seen.update(p.get('shortcode') for p in new)
        if not new:
            return False
        dates = [p.get('taken_at') for p in new]
        reliable = (len(new) >= 3 and all(p.get('is_pinned') is False for p in new)
                    and all(isinstance(t, int) and t > 0 for t in dates)
                    and dates == sorted(dates, reverse=True)
                    and all(not recent_post(p, self.cutoff) for p in new))
        self.old_batches = self.old_batches + 1 if reliable else 0
        return self.old_batches >= 2
