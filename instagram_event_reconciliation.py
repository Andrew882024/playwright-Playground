"""Conservative child identity matching and atomic Instagram analysis persistence.

No engine/session is created here. The caller supplies a session factory; database
models are imported only when persisting. Matching never uses array positions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


class ReconciliationError(ValueError):
    """There is insufficient evidence to change child identities safely."""


def normalized_title(title: str) -> str:
    return " ".join(title.casefold().split())


def utc_start(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReconciliationError("Event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def reconcile_events(
    existing: list[Any], incoming: list[dict[str, Any]],
) -> tuple[list[tuple[Any, dict[str, Any]]], list[dict[str, Any]], list[Any]]:
    """Return updates, inserts, removals without mutating any input.

    Exact normalized title/start matches take precedence. Remaining candidates
    must share title OR a non-null start, uniquely in both directions. Unresolved
    old AND new children make the entire result ambiguous, not a replacement.
    """
    old = {row.id: row for row in existing}
    if len(old) != len(existing):
        raise ReconciliationError("Duplicate existing child IDs")
    new = dict(enumerate(incoming))  # Internal bookkeeping only, never identity.
    old_keys = {key: (normalized_title(row.event_title), utc_start(row.event_start_at))
                for key, row in old.items()}
    new_keys = {key: (normalized_title(row['event_title']), utc_start(row.get('event_start_at')))
                for key, row in new.items()}
    if len(set(new_keys.values())) != len(new_keys):
        raise ReconciliationError("Duplicate incoming title/start identities")
    for values in incoming:
        utc_start(values.get('event_end_at'))
    matches: list[tuple[Any, dict[str, Any]]] = []

    def match_round(exact: bool) -> None:
        candidates: dict[int, list[int]] = {}
        for nk in new:
            title, start = new_keys[nk]
            candidates[nk] = [ok for ok in old if (
                old_keys[ok] == (title, start) if exact else
                old_keys[ok][0] == title or
                (start is not None and old_keys[ok][1] == start)
            )]
        reverse = {ok: [nk for nk, choices in candidates.items() if ok in choices] for ok in old}
        if exact and any(len(choices) > 1 for choices in candidates.values()):
            raise ReconciliationError("Multiple existing children have the same title/start")
        pairs = [(nk, choices[0]) for nk, choices in candidates.items()
                 if len(choices) == 1 and len(reverse[choices[0]]) == 1]
        for nk, ok in pairs:
            matches.append((old.pop(ok), new.pop(nk)))

    match_round(exact=True)
    match_round(exact=False)
    if old and new:
        raise ReconciliationError(
            "Cannot safely match changed events to existing children; previous analysis retained"
        )
    return matches, list(new.values()), list(old.values())


def persist_analysis(
    session_factory: Callable,
    profile_username: str,
    post_shortcode: str,
    parent_values: dict[str, Any],
    child_values: list[dict[str, Any]],
    *,
    reanalyze: bool = False,
) -> bool:
    """Lock parent, reconcile children and update parent in one transaction.

    Returns False if a competing worker already completed a non-reanalysis job.
    Any error propagates through the transaction context and rolls back all writes.
    Gemini/image network work must have completed before entering this function.
    """
    from sqlalchemy import select
    from Blueprint_db import InstagramEvents, InstagramPosts

    with session_factory() as session:
        with session.begin():
            parent = session.execute(
                select(InstagramPosts).where(
                    InstagramPosts.profile_username == profile_username.lstrip('@').strip(),
                    InstagramPosts.post_shortcode == post_shortcode,
                ).with_for_update()
            ).scalar_one()
            if parent.ai_analyzed and not reanalyze:
                return False
            existing = list(session.scalars(
                select(InstagramEvents).where(InstagramEvents.instagram_post_id == parent.id)
                .order_by(InstagramEvents.id)
            ).all())
            updates, inserts, removals = reconcile_events(existing, child_values)
            from app.instagram_post_presentation import canonical_cover_inputs
            before_cover = canonical_cover_inputs(parent, existing)
            parent_snapshot = {key: getattr(parent, key, None) for key in (
                'id', 'collection_title', 'provider_name', 'profile_username', 'post_shortcode')}
            parent_snapshot.update(parent_values)
            after_cover = canonical_cover_inputs(parent_snapshot, [
                dict(values, instagram_post_id=parent.id, id=index)
                for index, values in enumerate(child_values)])
            source_changed = any(
                key in parent_values and getattr(parent, key, None) != parent_values[key]
                for key in ('main_image_url', 'own_s3_url_for_main_image')
            )
            if source_changed or before_cover != after_cover:
                parent.generated_image_url = None
                parent.generated_image_input_hash = None
            for row, values in updates:
                changed = any(getattr(row, key) != value for key, value in values.items())
                cover_changed = source_changed or row.event_title != values['event_title']
                for key, value in values.items():
                    setattr(row, key, value)
                if cover_changed:
                    row.generated_image_url = None
                    row.generated_image_input_hash = None
                if changed:
                    row.is_duplicated = None  # A changed event needs fresh deduplication.
            for values in inserts:
                session.add(InstagramEvents(instagram_post_id=parent.id, **values))
            for row in removals:
                session.delete(row)
            if any(getattr(parent, key) != value for key, value in parent_values.items()
                   if key in ('event_title', 'provider_name', 'location', 'event_start_at', 'event_end_at')):
                parent.is_duplicated = None
            for key, value in parent_values.items():
                setattr(parent, key, value)
            parent.updated_at = datetime.now(timezone.utc)
        return True
