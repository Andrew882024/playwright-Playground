"""Explicit acknowledgment only. MySQL verification is always SELECT-only."""
import json
import re
import subprocess
from datetime import datetime,timezone
from app.event_master_sync.sync_service import SyncSettings,isolated_import_environment
from pathlib import Path

_REASONS={'no_activities','parent_marker_not_unique','legacy_conflict','unexpected_event_owner','fields_differ','verification_unavailable'}


class AcknowledgmentError(ValueError):
    pass


def verified_import(post_id):
    settings=SyncSettings.from_env();settings.validate()
    verifier=settings.backend_dir/'verify_instagram_parent_import.py'
    if not verifier.is_file():raise AcknowledgmentError('verification_unavailable')
    env=isolated_import_environment()
    env['INSTAGRAM_PLAYGROUND_DIR']=str(Path(__file__).resolve().parents[2])
    reply=subprocess.run([str(settings.python),str(verifier),'--post-id',str(post_id)],
        shell=False,cwd=str(settings.backend_dir),env=env,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=settings.timeout,check=False)
    raw=reply.stdout
    if not isinstance(raw,bytes) or len(raw)>4096:raise AcknowledgmentError('verification_unavailable')
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError()
            result[k]=v
        return result
    try:
        data=json.loads(raw,object_pairs_hook=unique)
        if reply.returncode==0 and set(data)=={'status','event_id','post_id','source_revision'}:
            if (data['status']=='verified' and type(data['post_id']) is int and data['post_id']==post_id
                and type(data['event_id']) is int and data['event_id']>0
                and isinstance(data['source_revision'],str) and re.fullmatch('[0-9a-f]{64}',data['source_revision'])):
                return data
        if set(data)=={'status','reason'} and data['status']=='rejected' and data['reason'] in _REASONS:
            raise AcknowledgmentError(data['reason'])
    except AcknowledgmentError:raise
    except Exception:pass
    raise AcknowledgmentError('verification_unavailable')


def acknowledge(post_id, *, write=False, store=None, verifier=verified_import):
    verified=verifier(post_id)
    if not write:
        # Repeat verification to reject changes observed between source snapshots.
        second=verifier(post_id)
        if second!=verified:raise AcknowledgmentError('verification_changed')
        return dict(status='dry-run',parent_id=post_id,event_id=verified['event_id'],action='acknowledge_only')
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert
    from Blueprint_db import InstagramPipelineRecovery as R,SessionLocal
    from app.pipeline_recovery.store import Store,revision
    from app.instagram_scraper.recency import scrape_cutoff
    store=store or Store(SessionLocal,scrape_cutoff())
    now=datetime.now(timezone.utc)
    with store.factory() as session,session.begin():
        snap=store.snapshot(session,post_id,lock=True)
        if snap is None:raise AcknowledgmentError('parent_ineligible')
        current=revision('event_master',*snap)
        if current!=verified['source_revision']:raise AcknowledgmentError('source_revision_changed')
        # Parent/child locks hold the PG source stable during this final verification.
        if verifier(post_id)!=verified:raise AcknowledgmentError('verification_changed')
        session.execute(insert(R).values(instagram_post_id=post_id,stage='event_master',source_revision=current,
            status='pending',attempt_count=0,failure_count=0).on_conflict_do_nothing())
        state=session.execute(select(R).where(R.instagram_post_id==post_id,R.stage=='event_master').with_for_update()).scalar_one()
        if state.lease_expires_at and state.lease_expires_at>now:raise AcknowledgmentError('active_lease')
        if state.status=='succeeded' and state.source_revision==current and state.acknowledged_revision==current:
            action='already_acknowledged'
        else:
            state.source_revision=current;state.acknowledged_revision=current;state.status='succeeded'
            state.failure_count=0;state.next_attempt_at=None;state.safe_reason=None
            state.lease_token=None;state.lease_expires_at=None
            action='acknowledged'
    return dict(status='succeeded',parent_id=post_id,event_id=verified['event_id'],action=action)
