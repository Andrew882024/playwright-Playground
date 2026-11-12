"""Explicit new-post IDs only; no scrape, covers, backfill or Event Master calls."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import json
from pathlib import Path


class Quiet:
    def write(self, value): return len(value)
    def flush(self): pass


def _execute_once(profile, parent_ids, *, authorize_retry=False):
    from sqlalchemy import select, text
    from sqlalchemy.exc import OperationalError, InterfaceError
    from Blueprint_db import InstagramPosts as P, SessionLocal
    from app.ai_analyze_service import gemini_analysis as ga, diagnostics
    from app.ai_analyze_service.ai_analyze import build_analyzer_post_dict
    from app.ai_analyze_service.http_budget import GeminiHttpBudget, capture as http_capture
    from app.parent_pipeline import daily_budget, attempt_safety
    from app.parent_pipeline.service import parent_lock
    from instagram_event_reconciliation import persist_analysis
    from google import genai

    ids=list(dict.fromkeys(parent_ids))
    if not 1 <= len(ids) <= 5 or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError('Select one to five explicit positive parent IDs')
    result=dict(status='completed',succeeded=0,failed=0,skipped=0,gemini_http_requests=0,posts=[])
    ledger=daily_budget.Ledger()
    # No hidden SDK retries, model fallback, JSON repair dispatch or automatic paid retry.
    client=genai.Client(api_key=ga.gemini_api_key(),http_options={'timeout':120000,'retry_options':{'attempts':1}})
    for pid in ids:
        detail=diagnostics.new_diagnostics();budget=GeminiHttpBudget(1)
        item=dict(parent_id=pid,status='failed');committed=False
        attempt=attempt_safety.Attempt(pid, authorize_retry=authorize_retry)
        try:
            with parent_lock(pid) as held:
                if not held:
                    item.update(status='skipped',reason='parent_busy')
                else:
                    with SessionLocal() as db:
                        db.execute(text('SET TRANSACTION READ ONLY'))
                        row=db.scalar(select(P).where(P.id==pid,P.profile_username==profile))
                        if row is None or row.ai_analyzed:
                            item.update(status='skipped',reason='missing_or_already_analyzed')
                        elif attempt_safety.review_required(pid) and not authorize_retry:
                            item.update(status='skipped',reason='previous_paid_attempt_requires_review')
                        else:
                            post=build_analyzer_post_dict(row)
                            shortcode=row.post_shortcode
                    if item['status']!='skipped':
                        urls=ga._image_urls_from_post(post)
                        post['first_image_url']=urls[0] if urls else None
                        with daily_budget.capture(ledger),http_capture(budget),attempt_safety.capture(attempt),diagnostics.capture(detail):
                            try:
                                ai,_=ga.analyze_post(client,ga.gemini_model_chain()[:1],post,ga.default_tz(),require_first_original=True)
                                diagnostics.phase('database_persistence')
                                committed=persist_analysis(SessionLocal,profile,shortcode,
                                    ga.ai_to_db_update_values(ai,ga.default_tz(),fallback_main_image_url=urls[0]),
                                    ga.ai_to_child_event_values(ai,ga.default_tz()),reanalyze=False)
                                item.update(status='succeeded' if committed else 'skipped',is_event=ai.is_event,child_events=len(ai.events))
                            except Exception as exc:
                                diagnostics.event('failure',exc)
                                raise
                            finally:
                                attempt.finish(committed=committed)
        except (OperationalError,InterfaceError):
            item.update(status='failed',reason='database_unavailable');result['status']='blocked'
        except Exception as exc:
            item.update(status='failed',reason='post_analysis_failed',exception_class=type(exc).__name__,
                        phase=detail.get('failure_phase'),http_status=detail.get('http_status'))
            if detail.get('http_status') in (401,403,429) or isinstance(exc,daily_budget.BudgetStopped):
                result['status']='blocked'
        item['request_diagnostics']=[e for e in detail['events'] if e['event']=='request_failed']
        item['input_summary']=detail.get('input_summary')
        item['gemini_http_requests']=budget.dispatched
        result['gemini_http_requests']+=budget.dispatched
        result[item['status'] if item['status']=='skipped' else 'succeeded' if item['status']=='succeeded' else 'failed']+=1
        result['posts'].append(item)
        if result['status']=='blocked':break
    result['unprocessed']=len(ids)-len(result['posts'])
    if result['status']!='blocked' and (result['failed'] or result['skipped']):result['status']='partial'
    result['daily_budget']=ledger.summary()
    return result



def execute(profile, parent_ids, *, retry_transient=False, progress=None):
    """Explicitly authorized retry scope: <=3 dispatches/post, same primary model.

    Every retry keeps the earlier journal entry and reserves the shared ledger.
    Unknown transport outcomes, invalid output, authentication and 400 never retry.
    """
    if not retry_transient:
        return _execute_once(profile, parent_ids)
    import time
    from app.parent_pipeline import attempt_safety
    ids=list(dict.fromkeys(parent_ids))
    if not 1 <= len(ids) <= 2 or any(type(i) is not int or not 2127 <= i <= 2137 for i in ids):
        raise ValueError('Retry pilot is restricted to two existing IDs in 2127..2137')
    result=dict(status='completed',succeeded=0,failed=0,skipped=0,gemini_http_requests=0,posts=[])
    for pid in ids:
        history=[]
        state=attempt_safety.inspect(pid)
        if state['review_required'] and state.get('last_request_state')!='failed':
            item=dict(parent_id=pid,status='skipped',reason='uncertain_prior_outcome_requires_review',gemini_http_requests=0)
        else:
            for number in range(3):
                one=_execute_once(profile,[pid],authorize_retry=True)
                item=one['posts'][0]
                history.append(dict(item))
                result['gemini_http_requests']+=one['gemini_http_requests']
                result['daily_budget']=one['daily_budget']
                if progress: progress(dict(parent_id=pid,attempt=number+1,status=item['status'],http_status=item.get('http_status'),gemini_http_requests=item['gemini_http_requests']))
                retryable=(item['status']=='failed' and item.get('http_status') in (429,500,502,503,504)
                           and item['gemini_http_requests']==1 and item.get('phase')=='gemini_request')
                if not retryable or number==2:break
                time.sleep(2 ** (number+1))
        item=dict(item,attempts=history,gemini_http_requests=sum(x['gemini_http_requests'] for x in history))
        result['posts'].append(item)
        result[item['status']]+=1
        if item.get('reason')=='database_unavailable' or item.get('http_status') in (401,403) or (history and one.get('status')=='blocked' and item.get('http_status') not in (429,500,502,503,504)):
            result['status']='blocked';break
    result['unprocessed']=len(ids)-len(result['posts'])
    if result['status']!='blocked' and (result['failed'] or result['skipped']):result['status']='partial'
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--post-id',type=int,action='append',required=True)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--retry-transient',action='store_true',help='Authorize up to three same-model requests for two existing pilot posts')
    args=parser.parse_args()
    if not 1<=len(set(args.post_id))<=5 or any(i<=0 for i in args.post_id):parser.error('Select at most five positive IDs')
    if not args.run:
        print(json.dumps(dict(status='selection_only',profile=args.profile,parent_ids=args.post_id,gemini_http_requests=0)));return
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    with redirect_stdout(Quiet()),redirect_stderr(Quiet()):result=execute(args.profile,args.post_id,retry_transient=args.retry_transient)
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
