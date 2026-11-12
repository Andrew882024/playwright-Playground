"""Stdlib-only discovery audit records. Never serialize arbitrary pipeline output."""
import fcntl
import json
import os
import re
import signal
import tempfile
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from app.instagram_scraper.scrape_health import CAPTURE_ERROR_CATEGORIES
from app.instagram_scraper.graphql_diagnostics import aggregate_diagnostics

STAGES = ('scraping','screening','gemini','original_image','cover','event_master')
COUNTS = ('inserted','updated','unchanged','processed','skipped','failed','review_required',
          'attempted','succeeded','eligible','candidates')
STATUSES = {'completed','partial_failure','failed','review_required','skipped','busy','no-op',
            'rejected','budget_exhausted','healthy','not_run','unknown','preflight','eligible','skipped_expired'}
REASONS = set('''login_required authentication_challenge unexpected_navigation navigation_blocked
navigation_or_browser_failed response_capture_failed no_valid_timeline_response timeline_verified
scrape_execution_failed scrape_unhealthy discovery_or_configuration_failed integration_already_running
parent_execution_failed parent_busy already_analyzed parent_missing_or_outside_recent_window
previous_analysis_outcome_requires_review extraction_not_committed no_activities already_uploaded
original_image_failed no_selected_original_image cover_disabled already_generated cover_failed
activity_date_review_required review_required skipped_expired global_gemini_budget_exhausted
global_cover_budget_exhausted daily_gemini_budget_exhausted daily_openai_cover_budget_exhausted
daily_paid_ledger_unavailable gemini_http_budget_exhausted gemini_http_request_failed
gemini_http_outcome_uncertain gemini_request_review_required parent_changed_or_missing
import_disabled import_unavailable import_skipped import_failed adoption-required migration-conflict
parent_pipeline_configuration_or_database_unavailable stored_parent_no_scrape
missing_caption_dates non_event_or_mixed_date_context event_intent_uncertain missing_event_dates
all_plausible_event_dates_expired inferred_event_dates_expired explicit_event_dates_expired
event_today_time_uncertain inferred_future_event_date explicit_future_event_date
missing_usable_image_references explicit_manual_review_override ambiguous_relative_context
unanchored_weekday invalid_event_date weekday_date_conflict numeric_date_order_uncertain
relative_publication_date_missing relative_absolute_date_conflict ambiguous_this_weekday
ambiguous_next_weekday missing_publication_year unresolved_or_ambiguous_dates
dependency_missing missing_local_file configuration_or_execution_failed termination_signal
keyboard_interrupt timeout execution_log_unavailable browser_dependency_missing'''.split())
_current = ContextVar('instagram_discovery_execution', default=None)
_ID = re.compile(r'[0-9a-f]{32}')


class LogUnavailable(RuntimeError):
    def __init__(self): super().__init__('execution_log_unavailable')


class Terminated(BaseException): pass


def utc(): return datetime.now(timezone.utc).isoformat()
def count(value): return value if type(value) is int and value >= 0 else 0
def reason(value): return value if isinstance(value,str) and value in REASONS else 'other'
def status(value): return value if isinstance(value,str) and value in STATUSES else 'unknown'


class Quiet:
    def write(self,value):return len(value)
    def flush(self):pass


def exception_info(exc):
    # Fixed categories only, never messages, traceback, module paths or repr(exc).
    if isinstance(exc,(ImportError,ModuleNotFoundError)): return dict(reason='dependency_missing',exception_class='ImportError')
    if isinstance(exc,FileNotFoundError): return dict(reason='missing_local_file',exception_class='FileNotFoundError')
    if isinstance(exc,TimeoutError): return dict(reason='timeout',exception_class='TimeoutError')
    # Inspect only a known browser-installation prefix; never retain the message/path.
    if type(exc).__module__.startswith('playwright.') and "Executable doesn't exist" in str(exc):
        return dict(reason='browser_dependency_missing',exception_class='BrowserError')
    return dict(reason='configuration_or_execution_failed',exception_class='Exception')


def budget_summary(raw=None):
    try:
        from app.parent_pipeline import daily_budget
        raw=raw if raw is not None else (daily_budget.current() or daily_budget.Ledger()).summary()
        if raw.get('status')!='available':return dict(status='unavailable',reason='daily_paid_ledger_unavailable')
        if datetime.strptime(raw['day'],'%Y-%m-%d').date().isoformat()!=raw['day']:raise ValueError()
        return dict(status='available',day=raw['day'],timezone='America/Los_Angeles',
            blocked_reason=reason(raw['blocked_reason']) if raw.get('blocked_reason') else None,
            providers={p:{k:count(raw['providers'][p].get(k)) for k in ('limit','used','remaining')}
                       for p in ('gemini','openai_cover')})
    except Exception:return dict(status='unavailable',reason='daily_paid_ledger_unavailable')


def _mkdir(directory):
    missing=[];cursor=directory
    while not cursor.exists():missing.append(cursor);cursor=cursor.parent
    for path in reversed(missing):
        try:path.mkdir(mode=0o700)
        except FileExistsError:pass
        _sync(path.parent)


def _sync(directory):
    fd=os.open(directory,os.O_RDONLY | os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class Record:
    def __init__(self,profile,headless):
        self.fd=None;self.broken=False;self.active=None;self.monotonic={};self.final_budget=None
        self.started=time.monotonic()
        self.directory=Path(os.getenv('INSTAGRAM_DISCOVERY_LOG_DIR') or
                            Path.home()/'.local/state/event-master/instagram-discovery-runs').expanduser()
        self.run_id=uuid4().hex
        try:
            if not self.directory.is_absolute() or self.directory.is_symlink():raise ValueError()
            _mkdir(self.directory)
            self.fd=os.open(self.directory/f'{self.run_id}.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            os.fsync(self.fd);_sync(self.directory)
            unfinished,active,unreadable=self.previous()
            selected=profile.strip().lstrip('@') if isinstance(profile,str) else None
            self.data=dict(version=1,run_id=self.run_id,started_at=utc(),ended_at=None,status='running',
                pipeline_status=None,profile=selected if selected and re.fullmatch(r'[A-Za-z0-9._]{1,30}',selected) else
                ('all_configured' if profile is None else 'invalid_profile'),headless=bool(headless),
                stages={name:dict(status='not_started',items=[]) for name in STAGES},counts={},parents=[],
                paid_requests_started=dict(gemini=0,openai_cover=0),
                previous_unfinished_run_ids=unfinished,active_run_ids=active,unreadable_prior_records=unreadable)
            self.save()
        except Exception:
            self.close()
            raise LogUnavailable() from None

    def previous(self):
        unfinished=[];active=[];unreadable=0
        for path in sorted(self.directory.glob('*.json')):
            if not _ID.fullmatch(path.stem):continue
            try:
                if path.is_symlink():raise ValueError()
                data=json.loads(path.read_text())
                if data.get('run_id')!=path.stem:raise ValueError()
                if data.get('status')!='running':continue
                fd=os.open(path.with_suffix('.lock'),os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:active.append(path.stem)
                    else:unfinished.append(path.stem)
                finally:os.close(fd)
            except Exception:unreadable+=1
        return unfinished,active,unreadable

    def save(self):
        temporary=None
        try:
            if self.broken:raise LogUnavailable()
            self.data['updated_at']=utc();self.data['daily_budget']=budget_summary(self.final_budget)
            fd,temporary=tempfile.mkstemp(prefix='.run-',dir=self.directory)
            with os.fdopen(fd,'w') as stream:
                json.dump(self.data,stream,sort_keys=True)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.directory/f'{self.run_id}.json');_sync(self.directory)
        except Exception:
            self.broken=True
            raise LogUnavailable() from None
        finally:
            try:
                if temporary and os.path.exists(temporary):os.unlink(temporary)
            except OSError:pass

    def close(self):
        if self.fd is not None:
            try:os.close(self.fd)
            except OSError:pass
            self.fd=None

    def _stop_active(self):
        if self.active is not None:
            name,index=self.active;item=self.data['stages'][name]['items'][index]
            if item['status']=='running':
                item.update(status='awaiting_outcome',ended_at=utc(),
                            elapsed_seconds=round(max(0,time.monotonic()-self.monotonic[(name,index)]),3))
            self.active=None

    def begin(self,name,parent_id=None):
        if name not in STAGES:return
        self._stop_active()
        entry=self.data['stages'][name];items=entry['items']
        item=dict(status='running',started_at=utc(),ended_at=None)
        if type(parent_id) is int and parent_id>0:item['parent_id']=parent_id
        items.append(item);entry['status']='running';self.active=(name,len(items)-1)
        self.monotonic[self.active]=time.monotonic();self.save()

    def finish_stage(self,name,summary,parent_id=None):
        if name not in STAGES:return
        entry=self.data['stages'][name]
        item=next((row for row in reversed(entry['items']) if row.get('parent_id')==parent_id),None)
        if item is None:
            item=dict(started_at=None,ended_at=utc())
            if parent_id is not None:item['parent_id']=parent_id
            entry['items'].append(item)
        if self.active and self.active[0]==name and entry['items'][self.active[1]] is item:self._stop_active()
        item.update({k:count(summary[k]) for k in COUNTS if k in summary})
        item['status']=('failed' if count(summary.get('failed')) or summary.get('status')=='failed' else
                        'review_required' if count(summary.get('review_required')) else
                        'skipped' if count(summary.get('skipped')) else 'completed')
        item['ended_at']=item.get('ended_at') or utc()
        if 'reason' in summary:item['reason']=reason(summary['reason'])
        # Strictly numeric safe diagnostics; no error messages or source URLs.
        health=summary.get('scrape_health',{})
        if isinstance(health,dict) and health:
            item.update(aggregate_diagnostics([health] if any(key in health for key in
                ('graphql_error_diagnostics','unsupported_timeline_shapes','timeline_wrapper_diagnostics')) else health.get('profiles',[])))
            item['health_reasons']=[reason(p.get('reason')) for p in health.get('profiles',[]) if isinstance(p,dict)]
            item['http_status_codes']=sorted({code for p in health.get('profiles',[]) if isinstance(p,dict)
                for code in p.get('http_status_codes',[]) if type(code) is int and 100<=code<=599})
            categories=health.get('error_categories')
            if not isinstance(categories,dict):
                categories={key:sum(count(p.get('error_categories',{}).get(key)) for p in health.get('profiles',[])
                            if isinstance(p,dict) and isinstance(p.get('error_categories',{}),dict))
                            for key in CAPTURE_ERROR_CATEGORIES}
            item['capture_error_categories']={key:count(categories.get(key)) for key in CAPTURE_ERROR_CATEGORIES}
            for key in ('capture_errors','graphql_responses','valid_timeline_responses',
                        'valid_empty_timeline_responses','ignored_graphql_responses'):
                if key in health:item[key]=count(health[key])
        diagnostics=summary.get('diagnostics',{})
        if isinstance(diagnostics,dict) and diagnostics:
            item['diagnostics']={key:diagnostics[key] for key in
                ('api_request_started','response_received','extraction_committed') if type(diagnostics.get(key)) is bool}
            code=diagnostics.get('http_status')
            if type(code) is int and 100<=code<=599:item['diagnostics']['http_status']=code
        for key in COUNTS:entry[key]=sum(count(row.get(key)) for row in entry['items'])
        entry['status']=('failed' if any(row['status']=='failed' for row in entry['items']) else
                         'review_required' if any(row['status']=='review_required' for row in entry['items']) else
                         'skipped' if all(row['status']=='skipped' for row in entry['items']) else 'completed')
        self.save()

    def paid(self,provider):
        if provider not in self.data['paid_requests_started']:raise LogUnavailable()
        self.data['paid_requests_started'][provider]+=1
        self.save()  # Before dispatch: a crash here is conservatively possible exposure.

    def parent(self,result):
        parent_id=result.get('parent_id')
        if type(parent_id) is not int or parent_id<=0:return
        self.data['parents'].append(dict(parent_id=parent_id,status=status(result.get('status')),
            reason=reason(result['reason']) if 'reason' in result else None))
        for name,summary in result.get('stages',{}).items():
            if name in STAGES and isinstance(summary,dict):self.finish_stage(name,summary,parent_id)
        self.save()

    def failure(self,exc):
        self.data['failure']=exception_info(exc)
        if self.active:
            name,index=self.active;parent_id=self.data['stages'][name]['items'][index].get('parent_id')
            self.finish_stage(name,dict(failed=1),parent_id)
        self.save()

    def finalize(self,result,interruption=None):
        active=self.active
        self._stop_active()
        if interruption and active:
            name,index=active
            self.data['stages'][name]['items'][index]['status']=interruption
            self.data['stages'][name]['status']=interruption
        self.final_budget=result.get('daily_budget')
        existing={p['parent_id'] for p in self.data['parents']}
        for item in result.get('parents',[]):
            parent_id=item.get('parent_id')
            if type(parent_id) is int and parent_id>0 and parent_id not in existing:
                self.data['parents'].append(dict(parent_id=parent_id,status=status(item.get('status')),
                    reason=reason(item['reason']) if 'reason' in item else None))
                existing.add(parent_id)
        self.data['counts']={k:count(result[k]) for k in COUNTS if k in result}
        self.data['pipeline_status']=status(result.get('status'))
        if 'reason' in result:self.data['reason']=reason(result['reason'])
        paid=any(self.data['paid_requests_started'].values())
        stages=self.data['stages'].values()
        failed=(count(result.get('failed'))>0 or any(s.get('failed',0) for s in stages) or
                result.get('status')=='failed' or (result.get('status')=='partial_failure' and not result.get('review_required')))
        review=(count(result.get('review_required'))>0 or result.get('status')=='review_required' or
                any(s.get('review_required',0) for s in self.data['stages'].values()))
        self.data['counts']['failed']=max(count(result.get('failed')),int(failed))
        self.data['counts']['review_required']=max(count(result.get('review_required')),int(review))
        self.data['status']=(interruption if interruption else 'busy' if result.get('status')=='busy' else
                             ('failed_after_paid_request' if paid else 'failed_before_paid_requests') if failed else
                             'completed_with_review_required' if review else 'completed')
        self.data['ended_at']=utc()
        self.data['elapsed_seconds']=round(max(0,time.monotonic()-self.started),3)
        self.data['summary']=(f"Instagram discovery {self.run_id}: {self.data['status']}; "
            f"processed={count(result.get('processed'))} failed={self.data['counts']['failed']} "
            f"review_required={self.data['counts']['review_required']}; "
            f"Gemini requests={self.data['paid_requests_started']['gemini']} "
            f"cover requests={self.data['paid_requests_started']['openai_cover']}")
        self.save()
        return dict(run_id=self.run_id,status=self.data['status'],summary=self.data['summary'],
                    previous_unfinished_run_ids=self.data['previous_unfinished_run_ids'])


def begin(name,parent_id=None):
    if _current.get():_current.get().begin(name,parent_id)


def finish(name,summary,parent_id=None):
    if _current.get():_current.get().finish_stage(name,summary,parent_id)


def parent_finished(result):
    if _current.get():_current.get().parent(result)


def failure(exc):
    if _current.get():_current.get().failure(exc)


def paid_request_started(provider):
    if _current.get():_current.get().paid(provider)


def invoke(function,*,profile=None,headless=False):
    """Create before imports/scraping; never change journals or retry anything."""
    if _current.get():return function()
    try:record=Record(profile,headless)
    except LogUnavailable:return dict(status='failed',reason='execution_log_unavailable')
    token=_current.set(record);old_handler=None;interruption=None
    def terminated(signum,frame):raise Terminated()
    try:
        if threading.current_thread() is threading.main_thread():
            old_handler=signal.signal(signal.SIGTERM,terminated)
        try:
            with redirect_stdout(Quiet()),redirect_stderr(Quiet()):result=function()
            if not isinstance(result,dict) or not isinstance(result.get('status'),str):raise ValueError()
        except (KeyboardInterrupt,Terminated) as exc:
            interruption='interrupted'
            result=dict(status='failed',reason='keyboard_interrupt' if isinstance(exc,KeyboardInterrupt) else 'termination_signal')
        except TimeoutError as exc:
            interruption='timed_out';record.failure(exc)
            result=dict(status='failed',reason='timeout')
        except (Exception,SystemExit) as exc:
            record.failure(exc)
            result=dict(status='failed',reason='dependency_missing' if isinstance(exc,ImportError) else 'discovery_or_configuration_failed')
        result['execution']=record.finalize(result,interruption)
        return result
    except (Exception,SystemExit):
        return dict(status='failed',reason='execution_log_unavailable',execution=dict(run_id=record.run_id,status='unfinished'))
    finally:
        if old_handler is not None:signal.signal(signal.SIGTERM,old_handler)
        _current.reset(token);record.close()
