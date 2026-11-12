"""Bounded collection -> automatic binding -> existing analysis-only executor.

No source attestation or import permission is manufactured. Production release
is deliberately not implemented until independent evidence and approval exist.
"""
from collections import Counter
from pathlib import Path
from app.instagram_scraper import analysis_batch, parent_binding, analysis_execution
from app.instagram_scraper.collection_staging import atomic, private_dir
from app.parent_pipeline import daily_budget
from app.instagram_scraper.daily_pilot import single_run_lock


SKIPS = {'already_imported','already_imported_acknowledgment','already_analyzed_or_extracted',
         'outside_recent_window','event_date_no_longer_eligible'}


def _blocked(exc):
    return str(exc) if isinstance(exc,parent_binding.Blocked) else 'candidate_processing_unavailable'


def _execute(root, *, profiles, run, max_parents, max_http_requests):
    ledger = daily_budget.current()
    prepared = analysis_batch.prepare_batch(root, limit=max_parents, profiles=profiles,
                                            live_parent_lookup=parent_binding.lookup_parents)
    result = dict(status='running' if run else 'dry_run', profiles=profiles,
        batch_id=prepared['batch_id'], candidates=[], exclusion_counts=prepared['exclusion_counts'],
        duplicate_observations_removed=prepared['duplicate_observations_removed'],
        parents_reused=0, parents_created=0, parents_would_be_created=0, parents_would_be_reused=0,
        candidate_count=len(prepared["candidates"]),
        ready_after_binding_for_approved_analysis=0, ready_for_paid_now=0,
        blocked_from_production_import=0, gemini_http_requests=0,
        gemini_max_http_requests=max_http_requests, gemini_http_slots_reserved=0,
        gemini_dispatch_count_uncertain=False, max_http_requests_per_parent=1,
        openai_requests=0, production_imports=0, analysis_succeeded=0, failed=0,
        skipped=0, review_required=0, database_writes=0,
        recovery='disabled', scheduler_enabled=False,
        production_handoff='existing_parent_pipeline_requires_independent_verification_and_import_approval')
    path = Path(root)/'analysis-batches'/(prepared['batch_id']+'.json')
    if run:
        private_dir(path.parent);atomic(path,prepared)
    stop_reason = None
    for record in prepared['candidates']:
        item = dict(candidate=record['index'],account=record['account'],shortcode=record['shortcode'],
            permalink=record['permalink'],import_authorized=False,source_authenticity_verified=False,
            source_uncertainties=record['uncertainty_reasons'],
            production_status='blocked_pending_independent_verification_and_import_approval')
        result['blocked_from_production_import'] += 1
        analysis_invoked = False
        try:
            preview = parent_binding.preview_record(root,record)
            item.update(parent_id=preview['parent_id'],binding=preview['status'])
            result['ready_after_binding_for_approved_analysis'] += 1
            if preview['parent_id'] is not None:result['parents_would_be_reused'] += 1
            else:result['parents_would_be_created'] += 1
            summary = ledger.summary()
            available = summary.get('status') == 'available' and summary['providers']['gemini']['remaining'] > 0
            if preview['parent_id'] is not None and available and max_http_requests > result['ready_for_paid_now']:
                result['ready_for_paid_now'] += 1
            if not run:
                item.update(status='planned_analysis_only',paid_budget_available=available)
                result['candidates'].append(item);continue
            if stop_reason:
                item.update(status='skipped',reason=stop_reason);result['skipped'] += 1
            elif max_http_requests and result['gemini_http_slots_reserved'] >= max_http_requests:
                item.update(status='skipped',reason='global_gemini_http_budget_exhausted');result['skipped'] += 1
            elif max_http_requests and not available:
                item.update(status='skipped' if summary.get('status')=='available' else 'review_required',
                    reason='daily_gemini_budget_exhausted' if summary.get('status')=='available' else 'daily_paid_ledger_unavailable')
                result['skipped' if item['status']=='skipped' else 'review_required'] += 1
            else:
                bound = parent_binding.bind_candidate(root,prepared['batch_id'],record['index'],write=True)
                item.update(parent_id=bound['parent_id'],binding='created' if bound['database_writes'] else 'reused',
                    analysis_batch_id=bound['analysis_batch_id'])
                result['parents_created'] += bound['database_writes']
                result['parents_reused'] += int(not bound['database_writes'])
                result['database_writes'] += bound['database_writes']
                if max_http_requests == 0:
                    item.update(status='bound_analysis_only',reason='paid_requests_disabled')
                else:
                    # Consume a batch slot BEFORE invocation; an unexpected
                    # exception must not replenish possible paid dispatches.
                    result['gemini_http_slots_reserved'] += 1
                    analysis_invoked = True
                    outcome = analysis_execution.execute(root,bound['analysis_batch_id'],bound['parent_id'],run=True)
                    dispatched = outcome['gemini_http_requests']
                    if type(dispatched) is not int or not 0 <= dispatched <= 1:
                        raise RuntimeError('Unexpected dispatch count')
                    result['gemini_http_requests'] += dispatched
                    analysis_invoked = False
                    item['analysis'] = outcome;item['status'] = outcome['status']
                    if item['status'] in ('analyzed_locally','reused_local_result'):result['analysis_succeeded'] += 1
                    elif item['status'] in ('review_required','interrupted'):result['review_required'] += 1
                    else:result['failed'] += 1
                    if outcome.get('diagnostics',{}).get('http_status') == 429:
                        stop_reason = 'provider_rate_limited_no_automatic_retry'
                    if item['status']=='interrupted':stop_reason='batch_interrupted'
        except (Exception,SystemExit,KeyboardInterrupt) as exc:
            if analysis_invoked:
                result['gemini_dispatch_count_uncertain'] = True
                stop_reason = 'analysis_outcome_requires_review'
            if isinstance(exc,KeyboardInterrupt):stop_reason='batch_interrupted'
            reason = _blocked(exc);skip = reason in SKIPS
            item.update(status='skipped' if skip else 'review_required',reason=reason)
            result['skipped' if skip else 'review_required'] += 1
        result['candidates'].append(item)
    result['reason_counts'] = dict(Counter(item.get('reason','analysis_only_planned' if not run else item['status'])
                                            for item in result['candidates']))
    if run:
        result['status'] = ('partial_failure' if result['failed'] else 'completed_with_review_required'
                            if result['review_required'] else 'completed')
    return result


@daily_budget.scoped
def execute(root, *, profiles=None, run=False, max_parents=5, max_http_requests=1,
            approve_parent_writes=False, approve_analysis_only=False,
            confirm_source_unverified=False, confirm_paid_requests=False):
    from app.instagram_scraper.collection_review import configured_accounts
    accounts = configured_accounts()
    profiles = accounts if profiles is None else profiles
    if not profiles or any(p not in accounts for p in profiles):raise ValueError('Configured accounts required')
    if type(max_parents) is not int or not 1 <= max_parents <= 5:raise ValueError('Bound one to five parents')
    if type(max_http_requests) is not int or not 0 <= max_http_requests <= 5:raise ValueError('Bound zero to five requests')
    flags=(approve_parent_writes,approve_analysis_only,confirm_source_unverified)
    if run and (not all(flags) or (max_http_requests>0 and not confirm_paid_requests)):
        raise ValueError('Explicit parent-write, untrusted-analysis and paid-request approval required')
    if not run and (any(flags) or confirm_paid_requests):raise ValueError('Approvals require explicit run')
    if not run:return _execute(root,profiles=profiles,run=False,max_parents=max_parents,max_http_requests=max_http_requests)
    # The existing OS lock serializes separate batch commands. Parent binding,
    # Gemini dispatch and daily reservations retain their own atomic locks.
    with single_run_lock(Path(root)/'ingestion-lock') as held:
        if not held:return dict(status='busy',reason='ingestion_batch_already_running',gemini_http_requests=0)
        return _execute(root,profiles=profiles,run=True,max_parents=max_parents,max_http_requests=max_http_requests)
