"""Scraper discovery + existing bounded parent runner. No automatic recovery/dashboard."""
import os
from contextlib import redirect_stdout,redirect_stderr
from sqlalchemy import select,text
from app.instagram_scraper.recency import scrape_cutoff,run_cutoff
from app.parent_pipeline.service import run as run_parent,DiscardUnstructuredOutput
from app.parent_pipeline.budget import RunBudget
from app.parent_pipeline.locks import advisory_lock
from app.parent_pipeline import daily_budget
from app.parent_pipeline import execution_log

# Stable distinct advisory-lock namespaces, shared by scheduled/manual integration runs.
RUN_LOCK=1701669001
PARENT_LOCK=1701669002


def configured_profiles(profile):
    from app.instagram_scraper.sync_ins import TARGET_URLS
    profiles=list(dict.fromkeys(TARGET_URLS))
    if profile is None:return profiles
    matches=[p for p in profiles if p.casefold()==profile.strip().lstrip('@').casefold()]
    if len(matches)!=1:raise ValueError('Unknown configured profile')
    return matches


def cap(name,requested):
    raw=(os.getenv(name) or '').strip()
    configured=int(raw) if raw else 1  # New entry point is bounded even if legacy setting is unlimited.
    if configured<0 or (requested is not None and requested<0):raise ValueError('Invalid cap')
    return min(configured,requested) if requested is not None else configured


def enabled(name):return (os.getenv(name) or '').strip().lower() in ('true','1','yes','on')


def candidates(profiles,cutoff,new_ids=(),limit=100,override_ids=()):
    from Blueprint_db import SessionLocal,InstagramPosts as P
    from app.ai_analyze_service.candidates import posted_at_utc_coalesce
    from app.ai_analyze_service.gemini_analysis import _image_urls_from_post
    with SessionLocal(autoflush=False) as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        # First query new identities so pending backlog cannot crowd out discovery.
        def query(new):
            stmt=select(P).where(P.profile_username.in_(profiles),P.ai_analyzed.is_(False),
                                posted_at_utc_coalesce()>=cutoff)
            stmt=stmt.where(P.id.in_(new_ids) if new else P.id.not_in(new_ids))
            from app.instagram_scraper.parent_binding import production_candidates_only
            stmt=stmt.where(production_candidates_only(P))
            return list(db.scalars(stmt.order_by(posted_at_utc_coalesce().desc(),P.id.desc()).limit(limit)).all())
        rows=(query(True) if new_ids else [])+query(False)
        from app.parent_pipeline.screening import eligible
        result=[]
        for parent in rows:
            screening=eligible(parent,override_ids)
            result.append(dict(parent_id=parent.id,account=parent.profile_username,
                eligible=screening['status']=='eligible',reason=screening['reason'],screening=screening,
                discovery='new_parent' if parent.id in new_ids else 'pending_parent'))
        return result[:limit]


@daily_budget.scoped
def execute(*,profile=None,run=False,gemini_cap=None,cover_cap=None,max_parents=10,override_ids=(),confirm_override=False,headless=False,best_effort=False):
    if bool(override_ids)!=bool(confirm_override) or any(type(i) is not int or i<=0 for i in override_ids):
        raise ValueError('Explicit parent IDs and separate review override confirmation are required together')
    if not 1<=max_parents<=100:raise ValueError('max-parents must be between 1 and 100')
    profiles=configured_profiles(profile);cutoff=scrape_cutoff()
    budget=RunBudget(cap('INSTAGRAM_PIPELINE_GEMINI_MAX_ATTEMPTS',gemini_cap),
                     cap('INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS',cover_cap))
    result=dict(status='preflight',profiles=profiles,cutoff=cutoff.isoformat(),
        recovery='disabled_for_this_command',dashboard='not_used',
        gemini_max_attempts=budget.gemini_remaining,cover_max_attempts=budget.cover_remaining,
        inserted=0,updated=0,unchanged=0,processed=0,skipped=0,failed=0,review_required=0,parents=[])
    result['headless']=headless
    result['scrape_health']=dict(status='not_run',profiles=[])
    result['cover_enabled']=enabled('INSTAGRAM_PIPELINE_GENERATE_COVERS')
    result['import_enabled']=enabled('INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER')
    if not run:
        result['candidates']=candidates(profiles,cutoff,limit=max_parents,**({'override_ids':override_ids} if override_ids else {}))
        result['review_required']=sum(item.get('screening',{}).get('status')=='review_required'
                                      for item in result['candidates'])
        result['discovery']='not_run; new Instagram posts are unknown until execution'
        return result
    # Serialize integrated runs before scraping the shared persistent Firefox profile.
    with advisory_lock(RUN_LOCK,1) as held:
        if not held:return dict(result,status='busy',reason='integration_already_running')
        with run_cutoff(cutoff),redirect_stdout(DiscardUnstructuredOutput()),redirect_stderr(DiscardUnstructuredOutput()):
            execution_log.begin('scraping')
            from app.instagram_scraper.sync_ins import main as scrape
            scrape_args=['--profile',profiles[0]] if profile is not None else []
            if headless:scrape_args.append('--headless')
            if best_effort:scrape_args.append('--best-effort')
            try:
                scraped=scrape(scrape_args)
            except Exception as exc:
                execution_log.failure(exc)
                # Browser startup, screenshots, and scrape DB failures must also
                # survive the console redirection without exposing raw errors.
                return dict(result,status='failed',reason='discovery_or_configuration_failed',failed=1,
                            scrape_health=dict(status='failed',reason='scrape_execution_failed',profiles=[]),
                            gemini_attempts=0,cover_attempts=0)
            for key in ('inserted','updated','unchanged'):result[key]=scraped.get(key,0)
            result['scrape_health']=scraped.get('scrape_health',dict(status='unknown',profiles=[]))
            execution_log.finish('scraping',dict(scraped,failed=1 if scraped.get('status','completed')!='completed' or
                                 result['scrape_health']['status']=='failed' else 0))
            if (scraped.get('status','completed') not in (('completed','partial') if best_effort else ('completed',))
                    or result['scrape_health']['status']=='failed'):
                return dict(result,status='failed',reason='scrape_unhealthy',failed=1,
                            gemini_attempts=0,cover_attempts=0)
            new_ids={i for p in scraped.get('profiles',[]) for i in p.get('inserted_parent_ids',[])}
            execution_log.begin('screening')
            selected=candidates(profiles,cutoff,new_ids,limit=max_parents,**({'override_ids':override_ids} if override_ids else {}))
            execution_log.finish('screening',dict(candidates=len(selected),eligible=sum(bool(p['eligible']) for p in selected),
                review_required=sum(p.get('screening',{}).get('status')=='review_required' for p in selected),
                skipped=sum(not p['eligible'] and p.get('screening',{}).get('status')!='review_required' for p in selected)))
            for item in selected:
                parent_id=item['parent_id']
                if not item['eligible'] or budget.gemini_remaining<=0:
                    review=item.get('screening',{}).get('status')=='review_required' or item['reason']=='previous_analysis_outcome_requires_review'
                    result['review_required' if review else 'skipped']+=1
                    result['parents'].append(dict(parent_id=parent_id,status='review_required' if review else 'skipped',reason=
                        item['reason'] if not item['eligible'] else 'global_gemini_budget_exhausted',screening=item.get('screening')))
                    continue
                try:
                    from app.parent_pipeline.screening import eligible
                    guard=lambda parent:eligible(parent,override_ids)
                    outcome=run_parent(parent_id,budget=budget,cutoff=cutoff,pending_only=True,eligibility_guard=guard,
                                       cover_enabled=result['cover_enabled'],import_enabled=result['import_enabled'])
                    outcome.setdefault('screening',item.get('screening'))
                    execution_log.parent_finished(outcome)
                    result['parents'].append(outcome)
                    if outcome['status'] in ('skipped','no-op','rejected'):
                        result['skipped']+=1;continue
                    result['processed']+=1
                    stages=outcome.get('stages',{}).values()
                    if any(s.get('review_required',0) for s in stages):result['review_required']+=1
                    if outcome['status']=='failed' or any(s.get('failed',0) for s in stages):result['failed']+=1
                except Exception as exc:
                    execution_log.failure(exc)
                    result['failed']+=1
                    result['parents'].append(dict(parent_id=parent_id,status='failed',reason='parent_execution_failed'))
        result['gemini_attempts']=budget.gemini_attempts;result['cover_attempts']=budget.cover_attempts
        # Screening review and exhausted budgets are expected queue outcomes.
        # Preserve partial failures reported by parent processing itself (including
        # downstream work requiring review), independently of screening counts.
        incomplete=any(p.get('status')=='partial_failure' for p in result['parents'])
        result['status']='partial_failure' if result['failed'] or incomplete or result['scrape_health'].get('status')=='partial' else 'completed'
        return result
