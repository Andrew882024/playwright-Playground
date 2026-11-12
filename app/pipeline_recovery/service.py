"""Optional bounded recovery and run-scoped normal-stage acknowledgments."""
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from app.pipeline_recovery.hooks import current_run
from app.pipeline_recovery.store import Store


def enabled(name):
    return (os.getenv(name) or '').strip().lower() in ('true','1','yes','on')


def nonnegative(name, default):
    value=int(os.getenv(name) or str(default))
    if value < 0: raise ValueError('Recovery caps must be nonnegative integers')
    return value


def classify(stage, result):
    status=result.get('status')
    if status in ('review_required','adoption-required','migration-conflict'):
        return 'review_required','manual_review'
    if status in ('succeeded','published') or result.get('mode') in ('generated','resumed','already-published'):
        return 'succeeded',None
    if result.get('reason') in ('source_password_missing','mysql_configuration_missing','mysql_host_not_local',
                                'importer_configuration_invalid','importer_result_invalid'):
        return 'review_required',result['reason']
    return 'failed','operation_failed'


@dataclass
class RecoveryRun:
    store: object
    cover_cap: int
    caps: dict
    active: dict
    attempted: dict = field(default_factory=lambda:{'cover':set(),'event_master':set()})
    claims: dict = field(default_factory=dict)
    recovery_attempts: dict = field(default_factory=lambda:{'cover':0,'event_master':0})
    cover_allowance_used: int = 0
    tracking_unavailable: bool = False

    @classmethod
    def configured(cls, cutoff):
        from Blueprint_db import SessionLocal
        failures=nonnegative('INSTAGRAM_PIPELINE_RECOVERY_MAX_FAILURES',5)
        if failures == 0: raise ValueError('Recovery max failures must be positive')
        return cls(Store(SessionLocal,cutoff,failures),
            nonnegative('INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS',1),
            dict(cover=nonnegative('INSTAGRAM_PIPELINE_COVER_RECOVERY_MAX_ATTEMPTS',1),
                 event_master=nonnegative('INSTAGRAM_PIPELINE_EVENT_MASTER_RECOVERY_MAX_ATTEMPTS',1)),
            dict(cover=enabled('INSTAGRAM_PIPELINE_RECOVER_COVERS') and enabled('INSTAGRAM_PIPELINE_GENERATE_COVERS'),
                 event_master=enabled('INSTAGRAM_PIPELINE_RECOVER_EVENT_MASTER') and enabled('INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER')))

    def begin_normal(self,stage,parent_id):
        if parent_id in self.attempted[stage]: return False
        self.attempted[stage].add(parent_id)
        try:
            claim=self.store.claim(stage,parent_id)
        except Exception:
            self.tracking_unavailable=True
            # Missing optional migration must not break existing normal operation.
            # Recovery is always fail-closed when state cannot be read/claimed.
            return not self.active[stage]
        if claim is None: return False
        self.claims[(stage,parent_id)]=claim
        if stage=='cover': self.cover_allowance_used+=1
        return True

    def finish_normal(self,stage,parent_id,result):
        claim=self.claims.pop((stage,parent_id),None)
        if claim is None: return
        status,reason=classify(stage,result)
        try:
            self.store.finish(claim,status,reason,result.get('source_revision'))
        except Exception: self.tracking_unavailable=True

    def recover(self,stage):
        result=dict(enabled=self.active[stage],max_attempts=self.caps[stage],attempted=0,recovered=0,skipped=0,skipped_expired=0,failed=0,review_required=0,parents=[])
        if not self.active[stage]:
            result['status']='disabled';return result
        try:
            candidates=self.store.candidates(stage,self.attempted[stage],limit=max(100,self.caps[stage]*10))
            for item in candidates:
                parent_id=item['parent_id'];reason=item['reason']
                if reason!='incomplete':
                    outcome='review_required' if reason in ('review_required','retry_limit') else 'skipped'
                    result[outcome]+=1;result['parents'].append(dict(parent_id=parent_id,status=outcome,reason=reason));continue
                if self.recovery_attempts[stage] >= self.caps[stage]:
                    result['skipped']+=1;result['parents'].append(dict(parent_id=parent_id,status='skipped',reason='attempt_cap'));continue
                claim=self.store.claim(stage,parent_id)
                if claim is None:
                    result['skipped']+=1;continue
                self.attempted[stage].add(parent_id);result['attempted']+=1;self.recovery_attempts[stage]+=1
                try:
                    if stage=='cover':
                        from Blueprint_db import SessionLocal
                        from app.event_cover_service.parent_cover import run_parent_cover
                        from app.event_cover_service.openai_cover import Settings
                        allow=self.cover_allowance_used < self.cover_cap
                        def guard(snapshot, reserve=False):
                            from app.pipeline_recovery.activity_dates import generation_policy
                            from app.event_cover_service.openai_cover import CoverError
                            code=generation_policy(snapshot)
                            if code: raise CoverError('Automatic generation blocked by activity date policy.',reason=code)
                            if reserve:
                                if self.cover_allowance_used >= self.cover_cap:
                                    raise CoverError('Paid generation budget exhausted.',reason='generation_not_authorized')
                                self.cover_allowance_used+=1
                        outcome=run_parent_cover(SessionLocal,parent_id,Settings.from_env(),dry_run=False,allow_generation=allow,generation_guard=guard)
                    else:
                        from app.event_master_sync.sync_service import sync_posts
                        with run_recovery(None):
                            reply=sync_posts([parent_id])
                        outcome=reply['parents'][0] if reply.get('parents') else dict(status='failed',reason=reply.get('error'))
                    status,reason=classify(stage,outcome)
                    stored=self.store.finish(claim,status,reason,outcome.get('source_revision'))
                    bucket='recovered' if stored=='succeeded' else 'review_required' if stored=='review_required' else 'failed'
                except Exception as exc:
                    # CoverError.reason is authored internally; never inspect external text.
                    from app.event_cover_service.openai_cover import CoverError
                    code=exc.reason if isinstance(exc,CoverError) else None
                    status='review_required' if code in ('review_required','activity_date_review_required') else 'pending' if code in ('generation_not_authorized','skipped_expired') else 'failed'
                    reason='paid_budget_exhausted' if code=='generation_not_authorized' else 'manual_review' if status=='review_required' else 'operation_failed'
                    if code in ('skipped_expired','activity_date_review_required'): reason=code
                    stored=self.store.finish(claim,status,reason)
                    bucket='skipped' if code=='generation_not_authorized' else 'review_required' if stored=='review_required' else 'failed'
                if reason=='skipped_expired':
                    bucket='skipped';result['skipped_expired']+=1
                result[bucket]+=1
                result['parents'].append(dict(parent_id=parent_id,status='skipped_expired' if reason=='skipped_expired' else bucket,reason=reason))
            result['status']='partial_failure' if result['failed'] or result['review_required'] else 'completed'
        except Exception:
            result.update(status='error',error='recovery_state_unavailable')
        print('Instagram recovery: '+json.dumps(dict(stage=stage,**result),sort_keys=True))
        return result

    def preview(self):
        # SELECTs only: no claims, state inserts, cache locks or importer execution.
        result={stage:self.store.candidates(stage,set(),limit=100,preview=True) for stage in ('cover','event_master')}
        already_synchronized=[]
        for stage, items in result.items():
            already_synchronized.extend(dict(parent_id=item['parent_id'],stage=stage)
                                        for item in items if item['reason']=='already_current')
            result[stage]=[item for item in items if item['reason']!='already_current']
        result['already_synchronized']=already_synchronized
        from app.event_cover_service.openai_cover import Settings
        for item in result['cover']:
            hint=inspect_cache(Settings.from_env().cache_dir,item['parent_id'])
            if item['reason']=='incomplete':
                item.update(hint)
        return result


def inspect_cache(cache_dir, parent_id):
    """Preview metadata only; never rename a temp PNG or acquire a file lock."""
    directory=cache_dir/'parents'/str(parent_id)
    records=list(directory.glob('*/*/manifest.json'))
    if not records:
        return dict(expected_action='generate_if_budget_available')
    try:
        for path in records:
            record=json.loads(path.read_text())
            if not isinstance(record,dict): raise ValueError()
            png=(path.parent/'generated.png').is_file() or (path.parent/'generated.png.tmp').is_file()
            if record.get('state') in ('request_started','uncertain') and not png:
                return dict(reason='review_required',expected_action='manual_review_no_generation')
        return dict(expected_action='validate_saved_artifacts_then_resume_or_generate')
    except Exception:
        return dict(reason='review_required',expected_action='manual_review_no_generation')


@contextmanager
def run_recovery(run):
    token=current_run.set(run)
    try: yield run
    finally: current_run.reset(token)
