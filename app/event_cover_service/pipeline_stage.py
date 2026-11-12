"""Optional bounded cover stage for parents committed by this pipeline invocation."""
from __future__ import annotations

import json
import os

from sqlalchemy import select
from app.pipeline_recovery import hooks
from app.pipeline_recovery.activity_dates import require_generation_date

from Blueprint_db import InstagramEvents, InstagramPosts, SessionLocal
from app.event_cover_service.openai_cover import CoverError, Settings
from app.event_cover_service.parent_cover import run_parent_cover as run_cover


def parents_for_posts(parent_ids: set[int]) -> list:
    """Read only current-run parents; eligibility is checked before each attempt."""
    if not parent_ids:
        return []
    with SessionLocal() as session:
        return list(session.execute(
            select(InstagramPosts.id, InstagramPosts.generated_image_url,
                   InstagramPosts.main_image_url, InstagramPosts.own_s3_url_for_main_image)
            .where(InstagramPosts.id.in_(sorted(parent_ids)), InstagramPosts.ai_analyzed.is_(True),
                   select(InstagramEvents.id).where(
                       InstagramEvents.instagram_post_id == InstagramPosts.id).exists())
            .order_by(InstagramPosts.id.asc())
        ).all())


def _log_cover_failure(parent_id: int, exc: Exception) -> None:
    diagnostic = {'parent_id': parent_id}
    # Stage labels are internal identifiers, never arbitrary exception metadata.
    if getattr(exc, 'stage', None) == 'source_download':
        diagnostic['stage'] = 'source_download'
    if isinstance(exc, CoverError):
        # CoverError messages are authored by this service, without external bodies/URLs.
        diagnostic['error'] = str(exc)
    else:
        diagnostic['exception_class'] = type(exc).__name__
    print('Generated-cover failure: ' + json.dumps(diagnostic, sort_keys=True))


def run_optional_cover_stage(committed_parent_ids: set[int]) -> dict:
    """Never propagate cover/configuration errors into the extraction pipeline.

    Attempts count calls to run_cover, including failed/uncertain/recovery calls.
    Skipped includes existing covers, missing sources, parents over the cap,
    and a cover already published by another process. No historical retry scan.
    """
    enabled = (os.getenv('INSTAGRAM_PIPELINE_GENERATE_COVERS') or '').strip().lower() in (
        'true', '1', 'yes', 'on',
    )
    result = dict(enabled=enabled, status='disabled' if not enabled else 'completed',
                  attempted=0, published=0, skipped=0, failed=0, review_required=0, skipped_expired=0, parents=[])
    try:
        if enabled:
            try:
                cap = int((os.getenv('INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS') or '1').strip())
                if cap < 0:
                    raise ValueError()
            except ValueError:
                result.update(status='error', error='invalid_cover_attempt_cap')
                return result
            result['max_attempts'] = cap
            if not committed_parent_ids:
                return result
            rows = parents_for_posts(committed_parent_ids)
            settings = Settings.from_env()
            for parent_id, generated_url, main_url, original_url in rows:
                reason = None
                if (generated_url or '').strip():
                    reason = 'already_generated'
                elif not ((original_url or '').strip() or (main_url or '').strip()):
                    reason = 'missing_source'
                elif result['attempted'] >= cap:
                    reason = 'attempt_cap'
                if reason:
                    result['skipped'] += 1
                    result['parents'].append(dict(parent_id=parent_id, status='skipped', reason=reason))
                    continue
                if not hooks.begin('cover',parent_id):
                    result['skipped']+=1
                    result['parents'].append(dict(parent_id=parent_id,status='skipped',reason='state_blocked'))
                    continue
                result['attempted'] += 1
                try:
                    outcome = run_cover(SessionLocal, parent_id, settings,
                                        dry_run=False, allow_generation=True, generation_guard=require_generation_date)
                    hooks.finish('cover',parent_id,outcome)
                    if outcome['mode'] == 'already-published':
                        result['skipped'] += 1
                        result['parents'].append(dict(parent_id=parent_id, status='skipped', reason='already_generated'))
                    else:
                        result['published'] += 1
                        result['parents'].append(dict(parent_id=parent_id, status='published'))
                except CoverError as exc:
                    if exc.reason in ('skipped_expired','activity_date_review_required','review_required'):
                        review=exc.reason!='skipped_expired'
                        hooks.finish('cover',parent_id,dict(status='review_required' if review else 'pending'))
                        if review:result['review_required']+=1
                        else:result['skipped']+=1;result['skipped_expired']+=1
                        result['parents'].append(dict(parent_id=parent_id,status='review_required' if review else 'skipped_expired',reason=exc.reason))
                        continue
                    hooks.finish('cover',parent_id,dict(status='failed'))
                    _log_cover_failure(parent_id, exc)
                    result['failed'] += 1
                    result['parents'].append(dict(parent_id=parent_id, status='failed', reason='cover_failed'))
                except Exception as exc:
                    hooks.finish('cover',parent_id,dict(status='failed'))
                    _log_cover_failure(parent_id, exc)
                    result['failed'] += 1
                    result['parents'].append(dict(parent_id=parent_id, status='failed', reason='cover_failed'))
            if result['failed'] or result['review_required']:
                result['status'] = 'partial_failure'
    except Exception:
        result.update(status='error', error='cover_stage_setup_or_selection_failed')
    finally:
        print('Generated-cover stage: ' + json.dumps(result, sort_keys=True))
    return result
