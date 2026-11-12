"""One stored parent only. No scraper, FastAPI startup or recovery orchestration."""
from contextlib import redirect_stdout,redirect_stderr
from sqlalchemy import select,text
from app.instagram_scraper.recency import scrape_cutoff,run_cutoff
from app.pipeline_recovery.hooks import current_run
from app.pipeline_recovery.activity_dates import generation_policy,require_generation_date
from app.parent_pipeline import daily_budget
from app.parent_pipeline import execution_log


class DiscardUnstructuredOutput:
    """Legacy console text may contain URLs. Diagnostics use a separate safe channel."""
    def write(self,value):return len(value)
    def flush(self):pass


def stage(**values):
    return dict(attempted=0,succeeded=0,skipped=0,failed=0,review_required=0,**values)


def read_parent(parent_id,cutoff):
    from Blueprint_db import SessionLocal,InstagramPosts as P,InstagramEvents as E
    from app.ai_analyze_service.candidates import posted_at_utc_coalesce
    with SessionLocal(autoflush=False) as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        from app.instagram_scraper.parent_binding import production_candidates_only
        parent=db.execute(select(P).where(P.id==parent_id,posted_at_utc_coalesce()>=cutoff,
            production_candidates_only(P))).scalar_one_or_none()
        if parent is None:return None
        children=list(db.scalars(select(E).where(E.instagram_post_id==parent_id).order_by(E.id)).all())
        return parent,children


def cover_policy(parent,children):
    from app.event_cover_service.parent_cover import snapshot_from_rows
    if not children:return 'no_activities'
    try:return generation_policy(snapshot_from_rows(parent,children)) or 'eligible'
    except Exception:return 'source_or_extraction_unavailable'


@daily_budget.scoped
def preflight(parent_id,cutoff=None):
    from app.parent_pipeline import attempt_safety
    snapshot=read_parent(parent_id,cutoff or scrape_cutoff())
    if snapshot is None:return dict(status='rejected',reason='parent_missing_or_outside_recent_window',parent_id=parent_id)
    parent,children=snapshot
    return dict(status='preflight',parent_id=parent_id,account=parent.profile_username,
        analyzed=bool(parent.ai_analyzed),activity_count=len(children),
        activities=[dict(title=c.event_title,start_at=c.event_start_at.isoformat() if c.event_start_at else None,
                         end_at=c.event_end_at.isoformat() if c.event_end_at else None) for c in children],
        has_original_image=bool((parent.own_s3_url_for_main_image or '').strip() or (parent.main_image_url or '').strip()),
        has_parent_cover=bool((parent.generated_image_url or '').strip()),paid_cover_policy=cover_policy(parent,children),
        gemini_max_attempts=1,cover_max_attempts=1,scraper='not_run_stored_parent',
        recovery='disabled_for_this_command',event_master='parent_marker_upsert_only_when_run',
        gemini_attempt_safety=attempt_safety.inspect(parent_id),
        note='Preflight does not verify remote credentials, Event Master conflicts, or contact services.')


def _run(parent_id, *, budget=None, cutoff=None, pending_only=False, cover_enabled=True, import_enabled=True,
         eligibility_guard=None, authorize_gemini_retry=False, gemini_model=None):
    from app.parent_pipeline import attempt_safety
    from Blueprint_db import SessionLocal
    from app.ai_analyze_service.ai_analyze import main as analyze
    from app.add_our_own_s3_url.add_our_own_s3_url import run_backfill
    from app.event_cover_service.parent_cover import run_parent_cover
    from app.event_cover_service.openai_cover import Settings,CoverError
    from app.event_master_sync.sync_service import sync_posts
    cutoff=cutoff or scrape_cutoff();result=dict(parent_id=parent_id,stages={})
    stages=result['stages']
    token=current_run.set(None)  # No recovery leases, backlog selection or dashboard dependencies.
    try:
        with run_cutoff(cutoff),redirect_stdout(DiscardUnstructuredOutput()),redirect_stderr(DiscardUnstructuredOutput()):
            snapshot=read_parent(parent_id,cutoff)
            if snapshot is None:return dict(status='rejected',parent_id=parent_id,reason='parent_missing_or_outside_recent_window')
            parent,children=snapshot
            if pending_only and parent.ai_analyzed:
                return dict(status='skipped',reason='already_analyzed',**result)
            if eligibility_guard is not None and not parent.ai_analyzed:
                screening=eligibility_guard(parent)
                if screening['status']!='eligible':
                    review=screening['status']=='review_required'
                    blocked=stage(reason=screening['reason']);blocked['review_required' if review else 'skipped']=1
                    return dict(status='review_required' if review else 'skipped',parent_id=parent_id,
                                screening=screening,stages={'gemini':blocked},reason=screening['reason'])
            if not parent.ai_analyzed:
                if attempt_safety.review_required(parent_id) and not authorize_gemini_retry:
                    review=stage(reason='previous_analysis_outcome_requires_review');review['review_required']=1
                    return dict(status='review_required',reason='previous_analysis_outcome_requires_review',
                                stages={'gemini':review},parent_id=parent_id)
            if not parent.ai_analyzed and budget is not None and not budget.consume('gemini'):
                return dict(status='skipped',reason='global_gemini_budget_exhausted',**result)
            stages['scrape']=stage(reason='stored_parent_no_scrape');stages['scrape']['skipped']=1
            stats=dict(attempted=0,committed=0,failed=0)
            execution_log.begin('gemini',parent_id)
            if not parent.ai_analyzed:
                from app.ai_analyze_service import diagnostics as diagnostic
                details=diagnostic.new_diagnostics()
                committed=set()
                model_options={} if gemini_model is None else {'gemini_model_override':gemini_model}
                durable_attempt=attempt_safety.Attempt(parent_id,authorize_retry=authorize_gemini_retry)
                try:
                    with diagnostic.capture(details),attempt_safety.capture(durable_attempt):
                        analyze(['--post-id',str(parent_id),'--profile',parent.profile_username,'--limit','1','--sleep','0'],
                            committed_parent_ids=committed,max_attempts=1,analysis_stats=stats,**model_options)
                except (Exception,SystemExit) as exc:
                    with diagnostic.capture(details):diagnostic.event('failure',exc)
                    stats['failed']=max(1,stats['failed'])
                durable_attempt.finish(committed=parent_id in committed)
                stages['gemini']=stage();stages['gemini'].update(attempted=stats['attempted'],succeeded=stats['committed'],failed=stats['failed'])
                if details.get('selected_model') is not None:
                    stages['gemini']['selected_model']=details['selected_model']
                if stats['failed'] or parent_id not in committed:
                    stages['gemini']['diagnostics']=details
                if parent_id not in committed:
                    daily_stop=daily_budget.current().blocked_reason if daily_budget.current() else None
                    if daily_stop and details.get('exception_class')=='BudgetStopped' and durable_attempt.request_id is None:
                        unavailable=daily_stop=='daily_paid_ledger_unavailable'
                        stages['gemini'].update(failed=0,skipped=0 if unavailable else 1,
                            review_required=1 if unavailable else 0,reason=daily_stop)
                        for name in ('original_image','cover','event_master'):
                            stages[name]=stage(reason='extraction_not_committed');stages[name]['skipped']=1
                        return dict(status='review_required' if unavailable else 'skipped',reason=daily_stop,**result)
                    needs_review=attempt_safety.review_required(parent_id)
                    if needs_review:
                        stages['gemini'].update(review_required=1,reason='gemini_request_review_required')
                    from app.ai_analyze_service.http_budget import current as current_http_budget
                    request_budget=current_http_budget()
                    if request_budget is not None and request_budget.stopped_reason:
                        stages['gemini']['reason']=request_budget.stopped_reason
                        if request_budget.dispatched==0:
                            stages['gemini'].update(failed=0,skipped=1)
                    for name in ('original_image','cover','event_master'):
                        stages[name]=stage(reason='extraction_not_committed');stages[name]['skipped']=1
                    if request_budget is not None and request_budget.stopped_reason:
                        return dict(status='budget_exhausted' if request_budget.stopped_reason=='gemini_http_budget_exhausted'
                                    else 'failed',reason=request_budget.stopped_reason,**result)
                    if needs_review:
                        return dict(status='review_required',reason='gemini_request_review_required',**result)
                    return dict(status='failed' if stats['failed'] else 'no-op',**result)
                snapshot=read_parent(parent_id,cutoff)
                if snapshot is None:return dict(status='failed',reason='parent_changed_or_missing',**result)
                parent,children=snapshot
            else:
                stages['gemini']=stage(reason='already_analyzed');stages['gemini']['skipped']=1
            execution_log.finish('gemini',stages['gemini'],parent_id)
            if not children:
                for name in ('original_image','cover','event_master'):
                    stages[name]=stage(reason='no_activities');stages[name]['skipped']=1
                return dict(status='completed',**result)
            image=stages['original_image']=stage()
            execution_log.begin('original_image',parent_id)
            if (parent.own_s3_url_for_main_image or '').strip():image.update(skipped=1,reason='already_uploaded')
            else:
                try:
                    report={};run_backfill(profile=parent.profile_username,limit=1,dry_run=False,post_id=parent_id,report=report)
                    image.update(report)
                    if not image['attempted']:image.update(skipped=1,reason='no_selected_original_image')
                except Exception:image.update(attempted=1,failed=1,reason='original_image_failed')
            execution_log.finish('original_image',image,parent_id)
            cover=stages['cover']=stage()
            execution_log.begin('cover',parent_id)
            try:
                if not cover_enabled:
                    raise CoverError('Cover stage disabled.',reason='cover_disabled')
                if budget is not None and not budget.consume('cover'):
                    raise CoverError('Global cover attempt budget exhausted.',reason='global_cover_budget_exhausted')
                cover['attempted']=1
                outcome=run_parent_cover(SessionLocal,parent_id,Settings.from_env(),dry_run=False,
                                        allow_generation=True,generation_guard=require_generation_date)
                if outcome['mode']=='already-published':cover.update(skipped=1,reason='already_generated')
                else:cover['succeeded']=1
            except daily_budget.BudgetStopped as exc:
                if exc.reason=='daily_paid_ledger_unavailable':cover.update(review_required=1,reason=exc.reason)
                else:cover.update(skipped=1,reason=exc.reason)
            except CoverError as exc:
                if exc.reason in ('skipped_expired','cover_disabled','global_cover_budget_exhausted'):cover.update(skipped=1,reason=exc.reason)
                elif exc.reason in ('activity_date_review_required','review_required'):cover.update(review_required=1,reason=exc.reason)
                else:cover.update(failed=1,reason='cover_failed')
            except Exception:cover.update(failed=1,reason='cover_failed')
            execution_log.finish('cover',cover,parent_id)
            imported=stages['event_master']=stage()
            execution_log.begin('event_master',parent_id)
            if not import_enabled:
                imported.update(skipped=1,reason='import_disabled')
                execution_log.finish('event_master',imported,parent_id)
                status='partial_failure' if any(s['failed'] or s['review_required'] for s in stages.values()) else 'completed'
                return dict(status=status,**result)
            try:
                reply=sync_posts([parent_id])  # No adoption argument; marker identity is preserved.
                imported['attempted']=reply['attempted']
                items=reply.get('parents',[])
                if not items:imported.update(failed=1,reason='import_unavailable')
                else:
                    item=items[0];status=item['status']
                    if status=='succeeded':imported.update(succeeded=1,action=item.get('action'),event_id=item.get('event_id'))
                    elif status in ('adoption-required','migration-conflict'):imported.update(review_required=1,reason=status)
                    elif status=='skipped':imported.update(skipped=1,reason='import_skipped')
                    else:imported.update(failed=1,reason='import_failed')
            except Exception:imported.update(attempted=1,failed=1,reason='import_failed')
            execution_log.finish('event_master',imported,parent_id)
    finally:current_run.reset(token)
    status='partial_failure' if any(s['failed'] or s['review_required'] for s in stages.values()) else 'completed'
    return dict(status=status,**result)


from app.parent_pipeline.locks import advisory_lock


def parent_lock(parent_id):
    return advisory_lock(1701669002,parent_id)


@daily_budget.scoped
def run(parent_id, *, budget=None, cutoff=None, pending_only=False, cover_enabled=True, import_enabled=True,
        eligibility_guard=None, gemini_max_http_requests=None, authorize_gemini_retry=False, confirm_gemini_retry=False,
        gemini_model=None):
    if gemini_model is not None:
        from app.ai_analyze_service.model_selection import validate_model
        gemini_model=validate_model(gemini_model)
    if bool(authorize_gemini_retry)!=bool(confirm_gemini_retry):
        raise ValueError('Gemini retry authorization requires separate confirmation.')
    from app.ai_analyze_service.http_budget import GeminiHttpBudget,capture
    requests=GeminiHttpBudget(gemini_max_http_requests)
    try:
        with capture(requests),parent_lock(parent_id) as held:
            if not held:result=dict(status='skipped',parent_id=parent_id,reason='parent_busy',stages={})
            else:result=_run(parent_id,budget=budget,cutoff=cutoff,pending_only=pending_only,
                            cover_enabled=cover_enabled,import_enabled=import_enabled,eligibility_guard=eligibility_guard,
                            authorize_gemini_retry=authorize_gemini_retry,gemini_model=gemini_model)
    except (Exception,SystemExit):
        if not requests.strict:raise
        # Keep dispatch accounting even if a later database/lock operation fails.
        result=dict(status='failed',parent_id=parent_id,
                    reason='parent_pipeline_configuration_or_database_unavailable')
    result.update(gemini_http_requests=requests.dispatched,gemini_max_http_requests=requests.limit)
    if gemini_model is not None:
        result.update(gemini_model_override=gemini_model,gemini_automatic_model_fallback=False)
    return result
