"""Explicit staged analysis only. Production reads; private local output; no import."""
from contextlib import redirect_stdout, redirect_stderr, ExitStack, nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from app.instagram_scraper.analysis_batch import analyzer_input
from app.instagram_scraper.collection_staging import atomic, digest, private_dir, window
from app.parent_pipeline import attempt_safety, daily_budget
from app.parent_pipeline.service import parent_lock, DiscardUnstructuredOutput
from app.ai_analyze_service import diagnostics, http_budget


class Blocked(RuntimeError):
    pass


def load_input(root, batch_id, parent_id):
    if not re.fullmatch('[a-f0-9]{64}', batch_id) or type(parent_id) is not int or parent_id <= 0:
        raise Blocked('invalid_batch_or_parent')
    path = Path(root) / 'analysis-batches' / (batch_id + '.json')
    if path.is_symlink():
        raise Blocked('unsafe_manifest')
    manifest = json.loads(path.read_text())
    expected = digest(dict(version=1, purpose='analysis_only_not_import', records=manifest['candidates']))
    if manifest.get('batch_id') != batch_id or expected != batch_id:
        raise Blocked('batch_revision_changed')
    matches = [r for r in manifest['candidates'] if r.get('parent_id') == parent_id]
    if len(matches) != 1:
        raise Blocked('parent_binding_missing_or_ambiguous')
    record = matches[0]
    object_id = record['source_object']
    if not re.fullmatch('[a-f0-9]{64}', object_id):
        raise Blocked('invalid_source_object')
    source = Path(root) / 'objects' / (object_id + '.json')
    if source.is_symlink():
        raise Blocked('unsafe_source_object')
    media = json.loads(source.read_text())
    if digest(media) != object_id:
        raise Blocked('staged_object_changed')
    post = analyzer_input(media, record['account'])
    if digest(post) != record['analyzer_input_hash'] or post['shortcode'] != record['shortcode']:
        raise Blocked('analysis_input_changed')
    return record, post


def authoritative_state(parent_id, record, post):
    """Fresh READ ONLY transaction on every check. No create_all or row writes.

    Check all local canonical-shortcode aliases, child rows and import recovery
    acknowledgments, rather than trusting the batch's dated export.
    """
    from sqlalchemy import select, text
    from Blueprint_db import SessionLocal, InstagramPosts as P, InstagramEvents as E, InstagramPipelineRecovery as R
    with SessionLocal(autoflush=False) as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        parents = list(db.scalars(select(P).where(P.post_shortcode == record['shortcode'])).all())
        selected = [p for p in parents if p.id == parent_id]
        if len(selected) != 1 or len(parents) != 1:
            raise Blocked('canonical_parent_missing_or_ambiguous')
        parent = selected[0]
        if parent.profile_username != record['account']:
            raise Blocked('authoritative_parent_identity_changed')
        if any(p.ai_analyzed for p in parents):
            raise Blocked('already_analyzed')
        if db.scalar(select(E.id).where(E.instagram_post_id == parent_id).limit(1)) is not None:
            raise Blocked('existing_extraction_requires_review')
        recovery = db.get(R, (parent_id, 'event_master'))
        if recovery is not None and recovery.acknowledged_revision:
            raise Blocked('already_imported_acknowledgment')
        # Do not analyze a staged caption/publication revision different from the
        # canonical parent. Source authenticity is STILL not asserted by this check.
        stamp = parent.posted_time
        unix = parent.posted_unix_seconds
        if stamp is not None:
            if stamp.utcoffset() is None:
                raise Blocked('authoritative_publication_unreliable')
            stamp_unix = int(stamp.timestamp())
            if unix is not None and unix != stamp_unix:
                raise Blocked('authoritative_publication_conflict')
            unix = stamp_unix
        if unix != post['taken_at'] or parent.caption != post['caption']:
            raise Blocked('authoritative_input_revision_changed')
    now = datetime.now(timezone.utc)
    if not window(now)[1].timestamp() <= post['taken_at'] <= now.timestamp() + 300:
        raise Blocked('outside_recent_window')
    from app.parent_pipeline.screening import screen
    screening = screen(dict(caption=post['caption'], posted_unix_seconds=post['taken_at']), now=now)
    if screening['status'] != 'eligible':
        raise Blocked('event_date_no_longer_eligible')
    return dict(status='verified_current_unanalyzed', source_authenticity_verified=False,
                mysql_import_lookup='not_performed_analysis_only')


def _available(ledger, attempt):
    attempt.check_available()
    summary = ledger.summary()
    if summary.get('status') != 'available':
        raise Blocked('daily_paid_ledger_unavailable')
    if summary['providers']['gemini']['remaining'] < 1:
        raise Blocked('daily_gemini_budget_exhausted')
    return summary


def execute(root, batch_id, parent_id, *, run=False):
    """No manual-retry bypass here. A prior noncommitted request requires review."""
    budget = http_budget.GeminiHttpBudget(1)
    details = diagnostics.new_diagnostics()
    result = dict(parent_id=parent_id, batch_id=batch_id, status='failed',
                  gemini_http_requests=0, gemini_max_http_requests=1,
                  production_database_writes=0, import_authorized=False,
                  source_authenticity_verified=False, extraction_committed=False)
    attempt = None
    ledger = None
    client = None
    locks = ExitStack()
    try:
        with redirect_stdout(DiscardUnstructuredOutput()), redirect_stderr(DiscardUnstructuredOutput()):
            record, post = load_input(root, batch_id, parent_id)
            result['uncertainty_reasons'] = record['uncertainty_reasons']
            result['source_verification'] = record['source_verification']
            ledger = daily_budget.Ledger()
            with nullcontext(locks.enter_context(parent_lock(parent_id))) as held:
                if not held:
                    raise Blocked('parent_busy')
                result['authoritative_state'] = authoritative_state(parent_id, record, post)
                output = Path(root) / 'analysis-results' / batch_id / str(parent_id) / 'result.json'
                if output.exists():
                    if output.is_symlink():
                        raise Blocked('unsafe_local_result')
                    saved = json.loads(output.read_text())
                    if (saved.get('analyzer_input_hash') != record['analyzer_input_hash'] or
                            saved.get('batch_id') != batch_id or saved.get('parent_id') != parent_id or
                            saved.get('status') != 'analyzed_locally'):
                        raise Blocked('local_result_revision_conflict')
                    result.update(status='reused_local_result', result_path=str(output))
                    return result
                attempt = attempt_safety.Attempt(parent_id)
                _available(ledger, attempt)
                if not run:
                    result.update(status='preflight', reason='explicit_run_and_confirmation_required')
                    return result
                # Validate private writable output before any network/paid dispatch.
                directory = private_dir(private_dir(private_dir(Path(root) / 'analysis-results') / batch_id) / str(parent_id))
                atomic(directory / 'intent.json', dict(parent_id=parent_id, batch_id=batch_id,
                    analyzer_input_hash=record['analyzer_input_hash'], status='prepared',
                    import_authorized=False, source_authenticity_verified=False))
                from app.ai_analyze_service import gemini_analysis as ga
                def recheck():
                    # Images can take time to download: check again immediately
                    # before dispatch. The ledger reservation is atomic at dispatch.
                    load_input(root, batch_id, parent_id)
                    authoritative_state(parent_id, record, post)
                    _available(ledger, attempt)
                with diagnostics.capture(details), http_budget.capture(budget), \
                        attempt_safety.capture(attempt), daily_budget.capture(ledger):
                    client = ga.genai.Client(api_key=ga.gemini_api_key(), http_options={
                        'timeout': 120000, 'retry_options': {'attempts': 1}})
                    ai, _ = ga.analyze_post(client, ga.gemini_model_chain(), post, ga.default_tz(),
                                           require_first_original=True, before_request=recheck)
                    saved = dict(status='analyzed_locally', parent_id=parent_id, batch_id=batch_id,
                        analyzer_input_hash=record['analyzer_input_hash'], source_object=record['source_object'],
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        analysis=ai.model_dump(mode='json'), uncertainty_reasons=record['uncertainty_reasons'],
                        source_verification=record['source_verification'], source_authenticity_verified=False,
                        import_authorized=False, extraction_committed=False,
                        gemini_http_requests=budget.dispatched)
                    atomic(output, saved)
                    result.update(status='analyzed_locally', result_path=str(output))
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        with diagnostics.capture(details):
            diagnostics.event('failure', exc)
        reason = str(exc) if isinstance(exc, Blocked) else 'analysis_failed'
        if isinstance(exc, attempt_safety.GeminiReviewRequired):
            reason = 'previous_analysis_outcome_requires_review'
        if isinstance(exc, daily_budget.BudgetStopped):
            reason = exc.reason
        if isinstance(exc, http_budget.GeminiRequestStopped):
            reason = budget.stopped_reason or 'gemini_http_request_stopped'
        result.update(status='review_required' if budget.dispatched or isinstance(exc, (Blocked, attempt_safety.GeminiReviewRequired)) else 'failed',
                      reason=reason, diagnostics=details)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            result['status'] = 'interrupted'
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass
        if attempt is not None:
            try: attempt.finish(committed=False)
            except Exception:
                result.update(status='review_required', reason='attempt_finalization_failed')
        try:
            locks.close()
        except Exception:
            result.update(status='review_required', reason='parent_lock_release_failed')
        result['gemini_http_requests'] = budget.dispatched
        if ledger is not None:
            result['daily_budget'] = ledger.summary()
    return result
