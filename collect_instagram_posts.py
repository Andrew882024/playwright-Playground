#!/usr/bin/env python3
"""Bounded private staging only. Never an ingestion or paid pipeline entry point."""
import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

from app.instagram_scraper.collection_staging import StagingCollector, CollectionStop, atomic, private_dir
from app.instagram_scraper.daily_pilot import single_run_lock
from app.instagram_scraper.collection_review import configured_accounts,build_queue

ROOT=Path(__file__).resolve().parent
STAGING=ROOT/'.instagram-post-staging'
# No reviewed production handoff exists. A configuration flag cannot bypass it.
SCHEDULER_HANDOFF_VERIFIED=False


class Quiet:
    def write(self,value):return len(value)
    def flush(self):pass


def blocked(reason,profile='dvcatucsd'):
    return dict(status='skipped',reason=reason,profile=profile,database_writes=0,
                gemini_requests=0,openai_requests=0,production_handoff='disabled')


def worker(args,run_id):
    from app.instagram_scraper.schema_capture import capture
    profile=args.profile if isinstance(getattr(args,'profile',None),str) else 'dvcatucsd'
    with single_run_lock(STAGING) as acquired:
        if not acquired:return blocked('collection_already_running',profile)
        collector=StagingCollector(STAGING,run_id,args.mode,datetime.now(timezone.utc),profile=profile)
        if args.mode=='daily' and collector.state['collection_days'].get(profile)==collector.local.date().isoformat():
            collector.checkpoint('skipped','already_collected_today')
            return collector.summary('skipped','already_collected_today')
        try:
            with contextlib.redirect_stdout(Quiet()),contextlib.redirect_stderr(Quiet()):
                result=capture(profile,headless=True,collection=collector,
                               max_scrolls=args.max_scrolls,max_seconds=max(1,args.max_seconds-10))
            return collector.finish(result)
        except CollectionStop as exc:
            reason=str(exc) if str(exc) in ('metadata_limit','metadata_type_unsupported','metadata_size_limit',
                'staging_record_limit','staging_storage_limit','staging_processing_failed') else 'collection_stopped'
        except KeyboardInterrupt:reason='collection_interrupted'
        except (Exception,SystemExit):reason='collection_execution_failed'
        collector.checkpoint('failed',reason)
        return collector.summary('failed',reason)


def supervise(args):
    profile=args.profile if isinstance(getattr(args,'profile',None),str) else 'dvcatucsd'
    run_id=uuid.uuid4().hex
    argv=[sys.executable,str(Path(__file__).resolve()),'--worker',run_id,'--run','--profile',profile,
          '--mode',args.mode,'--max-scrolls',str(args.max_scrolls),'--max-seconds',str(args.max_seconds)]
    process=subprocess.Popen(argv,cwd=ROOT,shell=False,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
    interrupted=False
    try:
        output,_=process.communicate(timeout=max(1,args.max_seconds-5))
        result=json.loads(output)
        if not isinstance(result,dict):raise ValueError()
        return result
    except (subprocess.TimeoutExpired,KeyboardInterrupt):
        interrupted=True
        os.killpg(process.pid,signal.SIGINT)
        try:process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=1)
    except (ValueError,TypeError):pass
    # Read only our safe aggregate checkpoint. No raw media in terminal output.
    checkpoint=STAGING/'runs'/(run_id+'.summary.json')
    result=json.loads(checkpoint.read_text()) if checkpoint.exists() else blocked('worker_failed',profile)
    result.update(status='interrupted' if interrupted else 'failed',
                  reason='collection_time_limit_or_interrupt' if interrupted else 'worker_failed')
    if checkpoint.exists():atomic(checkpoint,result)
    return result



def collect_profiles(args):
    private_dir(STAGING)
    profiles=configured_accounts() if args.all_profiles else [args.profile]
    if len(profiles)==1:
        with single_run_lock(STAGING/'batch-lock') as held:
            return supervise(args) if held else blocked('collection_already_running')
    batch_id=uuid.uuid4().hex;out=dict(batch_id=batch_id,status='running',mode=args.mode,
        accounts=profiles,profiles=[],database_writes=0,gemini_requests=0,openai_requests=0,
        ready_for_import=0,production_handoff='disabled',scheduler_enabled=False)
    with single_run_lock(STAGING/'batch-lock') as held:
        if not held:return blocked('collection_already_running')
        runs=private_dir(STAGING/'runs')
        for profile in profiles:
            args.profile=profile
            try:
                result=supervise(args)  # Exactly one bounded attempt per account, no retry.
            except KeyboardInterrupt:result=dict(blocked('collection_interrupted',profile),status='interrupted')
            except (Exception,SystemExit):result=dict(blocked('collection_start_failed',profile),status='failed')
            out['profiles'].append({key:result.get(key) for key in (
                'profile','run_id','status','reason','recent_candidates_collected',
                'unknown_timestamp_candidates','counts','media_counts','review_counts')})
            atomic(runs/(batch_id+'.batch.json'),out)
            print(json.dumps(dict(stage='collection',profile=profile,status=result['status'],
                collected=result.get('recent_candidates_collected',0))),file=sys.stderr,flush=True)
            if result['status']=='interrupted':break
        queue=build_queue(STAGING)
        atomic(STAGING/'review_queue.json',queue)
        out['review_counts']=queue['counts'];out['review_queue']=str(STAGING/'review_queue.json')
        out['status']='partial_failure' if any(p['status'] in ('failed','interrupted') for p in out['profiles']) else 'completed_review_only'
        atomic(runs/(batch_id+'.batch.json'),out)
    return out


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    profiles=parser.add_mutually_exclusive_group()
    profiles.add_argument('--profile',choices=configured_accounts())
    profiles.add_argument('--all-profiles',action='store_true')
    parser.add_argument('--mode',choices=['initial','daily'],default='initial')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight',action='store_true')
    modes.add_argument('--run',action='store_true')
    parser.add_argument('--max-scrolls',type=int,default=20)
    parser.add_argument('--max-seconds',type=int,default=300)
    parser.add_argument('--scheduled',action='store_true')
    ingestion=parser.add_mutually_exclusive_group()
    ingestion.add_argument('--ingest-staged',action='store_true',help='Reuse staged posts; automatically bind approved analysis-only parents')
    ingestion.add_argument('--collect-and-ingest',action='store_true',help='Collect using the existing collector, then run bounded staged analysis')
    parser.add_argument('--max-parents',type=int,default=5)
    parser.add_argument('--gemini-max-http-requests',type=int,default=1)
    parser.add_argument('--approve-parent-writes',action='store_true')
    parser.add_argument('--approve-analysis-only',action='store_true')
    parser.add_argument('--confirm-source-unverified',action='store_true')
    parser.add_argument('--confirm-paid-requests',action='store_true')
    parser.add_argument('--worker',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    args.profile=args.profile or "dvcatucsd"
    ingest=args.ingest_staged or args.collect_and_ingest
    approvals=(args.approve_parent_writes,args.approve_analysis_only,args.confirm_source_unverified)
    if ingest:
        if args.worker:parser.error('Ingestion cannot run in a collection worker')
        if not 1<=args.max_parents<=5 or not 0<=args.gemini_max_http_requests<=5:parser.error('Bound 1..5 parents and 0..5 requests')
        if args.run and (not all(approvals) or (args.gemini_max_http_requests>0 and not args.confirm_paid_requests)):
            parser.error('Run requires explicit parent-write, analysis-only, source-unverified and paid-request approvals')
        if not args.run and (any(approvals) or args.confirm_paid_requests):parser.error('Approvals require --run')
    elif any(approvals) or args.confirm_paid_requests:parser.error('Approval flags require an ingestion mode')
    if args.worker and args.all_profiles:parser.error("Worker requires a single profile")
    if not 0<=args.max_scrolls<=20 or not 10<=args.max_seconds<=300:
        parser.error('Require 0..20 scrolls and 10..300 seconds')
    if args.worker and not re.fullmatch('[0-9a-f]{32}',args.worker):parser.error('Invalid worker identifier')
    if args.scheduled and (os.getenv('INSTAGRAM_COLLECTION_SCHEDULE_ENABLED','').lower()!='true' or
                           not SCHEDULER_HANDOFF_VERIFIED):
        result=blocked('scheduler_disabled_pending_verified_handoff')
    elif ingest:
        from dotenv import load_dotenv
        load_dotenv(ROOT/'.env')
        from app.instagram_scraper.ingestion_batch import execute
        collection=None
        ingestion_started=False
        try:
            if args.collect_and_ingest and args.run:
                collection=collect_profiles(args)
                if collection['status'] not in ('completed_review_only','completed_staging_only','skipped'):
                    raise ValueError('Collection unsuccessful')
            ingestion_started=True
            result=execute(STAGING,profiles=configured_accounts() if args.all_profiles else [args.profile],
                run=args.run,max_parents=args.max_parents,max_http_requests=args.gemini_max_http_requests,
                approve_parent_writes=args.approve_parent_writes,approve_analysis_only=args.approve_analysis_only,
                confirm_source_unverified=args.confirm_source_unverified,confirm_paid_requests=args.confirm_paid_requests)
            result['collection']=collection or dict(status='not_run',reason='reuse_staged_candidates' if args.ingest_staged else 'dry_run_no_collection')
        except (Exception,SystemExit):
            uncertain=bool(ingestion_started and args.run)
            result=dict(status='failed',reason='staged_ingestion_unavailable',
                gemini_http_requests=None if uncertain else 0,gemini_dispatch_count_uncertain=uncertain)
            if collection is not None:result['collection']=collection
    elif not args.run:
        result=dict(status='preflight',profiles=configured_accounts() if args.all_profiles else [args.profile],profile=None if args.all_profiles else args.profile,mode=args.mode,headless=True,
                    max_scrolls=args.max_scrolls,max_seconds=args.max_seconds,max_age_days=40,
                    staging_directory=str(STAGING),scheduler_enabled=False,database_writes=0,
                    gemini_requests=0,openai_requests=0,production_handoff='disabled')
    else:
        try:result=worker(args,args.worker) if args.worker else collect_profiles(args)
        except (Exception,SystemExit):result=blocked('collection_initialization_failed',args.profile);result['status']='failed'
    print(json.dumps(result,sort_keys=True,separators=(',',':')))
    return 0 if result['status'] in ('preflight','skipped','completed_staging_only','completed_review_only','dry_run','completed','completed_with_review_required') else 1


if __name__=='__main__':raise SystemExit(main())
