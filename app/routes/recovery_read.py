"""Private SELECT-only recovery API; never imports recovery claim/action functions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import html
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, func, text

from app.service_auth import require_recovery_reader
from app.recovery_contract import Health, Configuration, Tasks, Parent, ImportPreview, Stage, Status, SafeReadRoute, Task, field_hash

def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Authorization'
    response.headers['X-Content-Type-Options'] = 'nosniff'


router = APIRouter(route_class=SafeReadRoute, prefix='/internal/recovery', dependencies=[Depends(require_recovery_reader), Depends(no_store)])


def get_read_db():
    try:
        from Blueprint_db import SessionLocal
        with SessionLocal(autoflush=False) as db:
            try:
                db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                yield db
            finally:
                db.rollback()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Recovery data unavailable; verify schema/configuration.') from None


@contextmanager
def safe_read():
    try:
        yield
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Recovery data unavailable; verify schema/configuration.') from None


def task_dto(row, current_revision=None):
    known = {'manual_review','operation_failed','retry_limit','skipped_expired','activity_date_review_required',
             'stale_revision','paid_budget_exhausted'}
    acknowledgment = ('not_acknowledged' if not row.acknowledged_revision else 'unknown' if current_revision is None
                      else 'current' if row.status == 'succeeded' and row.acknowledged_revision == current_revision
                      else 'stale')
    return Task(parent_id=row.instagram_post_id, stage=row.stage, status=row.status,
        attempt_count=row.attempt_count, failure_count=row.failure_count,
        last_attempt_at=row.last_attempt_at, next_attempt_at=row.next_attempt_at,
        lease_active=bool(row.lease_expires_at and row.lease_expires_at > datetime.now(timezone.utc)),
        acknowledgment=acknowledgment, reason=row.safe_reason if row.safe_reason in known else 'other' if row.safe_reason else 'none')


def source(db, parent_id):
    from Blueprint_db import InstagramPosts as P, InstagramEvents as E, InstagramPipelineRecovery as R
    parent = db.get(P, parent_id)
    if parent is None:
        raise HTTPException(404, 'Parent not found.')
    children = list(db.scalars(select(E).where(E.instagram_post_id == parent_id).order_by(E.id)).all())
    recovery = list(db.scalars(select(R).where(R.instagram_post_id == parent_id).order_by(R.stage)).all())
    return parent, children, recovery


def parent_dto(parent, children, recovery):
    from app.instagram_post_presentation import build_post_presentation, import_source_revision
    from app.pipeline_recovery.store import revision
    p = build_post_presentation(parent, children)
    return Parent(parent_id=parent.id, title=p['title'], provider=parent.provider_name or parent.profile_username,
        description=p['description'], location=p['location_summary'] or 'Location not provided',
        earliest_start=p['earliest_start'], multi_session=p['multi_session'],
        activities=[dict(id=a['id'],title=a['title'],provider=a['provider'],location=a['location'],
                         start=a['start'],end=a['end'],duration_in_minutes=a['duration_in_minutes']) for a in p['activities']],
        image_source='generated_parent' if parent.generated_image_url else 'original_s3' if parent.own_s3_url_for_main_image
                     else 'original' if parent.main_image_url else 'unavailable',
        source_revision=import_source_revision(parent, children),
        recovery=[task_dto(row, revision(row.stage,parent,children)) for row in recovery])


@router.get('/health', response_model=Health)
def health(db=Depends(get_read_db)):
    with safe_read():
        db.execute(text('SELECT 1'))
        ready = db.scalar(text("SELECT to_regclass('instagram_pipeline_recovery') IS NOT NULL"))
        return Health(recovery_schema_ready=bool(ready))


@router.get('/configuration', response_model=Configuration)
def configuration():
    def enabled(name): return os.getenv(name,'').strip().lower() in {'true','1','yes','on'}
    def cap(name, default): return int(os.getenv(name) or str(default))
    with safe_read():
        return Configuration(generate_covers_enabled=enabled('INSTAGRAM_PIPELINE_GENERATE_COVERS'),
            sync_event_master_enabled=enabled('INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER'),
            recover_covers_enabled=enabled('INSTAGRAM_PIPELINE_RECOVER_COVERS'),
            recover_event_master_enabled=enabled('INSTAGRAM_PIPELINE_RECOVER_EVENT_MASTER'),
            cover_attempt_cap=cap('INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS',1),
            cover_recovery_cap=cap('INSTAGRAM_PIPELINE_COVER_RECOVERY_MAX_ATTEMPTS',1),
            import_recovery_cap=cap('INSTAGRAM_PIPELINE_EVENT_MASTER_RECOVERY_MAX_ATTEMPTS',1),
            max_failures=cap('INSTAGRAM_PIPELINE_RECOVERY_MAX_FAILURES',5))


@router.get('/tasks', response_model=Tasks)
def tasks(page: int = Query(1,ge=1,le=10000), page_size: int = Query(25,ge=1,le=100),
          status: Status | None = None, stage: Stage | None = None, db=Depends(get_read_db)):
    from Blueprint_db import InstagramPipelineRecovery as R
    with safe_read():
        conditions = []
        if status is not None: conditions.append(R.status == status)
        if stage is not None: conditions.append(R.stage == stage)
        total = db.scalar(select(func.count()).select_from(R).where(*conditions))
        rows = db.scalars(select(R).where(*conditions).order_by(R.instagram_post_id,R.stage)
                          .offset((page-1)*page_size).limit(page_size)).all()
        items = []
        snapshots = {}
        from app.pipeline_recovery.store import revision
        for row in rows:
            if row.instagram_post_id not in snapshots:
                snapshots[row.instagram_post_id] = source(db,row.instagram_post_id)
            parent,children,_ = snapshots[row.instagram_post_id]
            items.append(task_dto(row,revision(row.stage,parent,children)))
        return Tasks(items=items,total=total,page=page,page_size=page_size)


@router.get('/parents/{parent_id}', response_model=Parent)
def parent_details(parent_id: int, db=Depends(get_read_db)):
    if parent_id <= 0: raise HTTPException(422,'Parent ID must be positive.')
    with safe_read():
        return parent_dto(*source(db,parent_id))


@router.get('/parents/{parent_id}/import-preview', response_model=ImportPreview)
def import_preview(parent_id: int, db=Depends(get_read_db)):
    if parent_id <= 0: raise HTTPException(422,'Parent ID must be positive.')
    from app.instagram_event_master_projection import build_event_master_fields
    with safe_read():
        parent,children,recovery = source(db,parent_id)
        projection_status, hashes = 'ready', {}
        try:
            fields = build_event_master_fields(vars(parent),[vars(child) for child in children])
            if fields is None: projection_status = 'no_activities'
            else: hashes = {key:field_hash(value) for key,value in fields.items()}
        except ValueError:
            projection_status = 'invalid_source'
        provenance = [f'Post shortcode: {html.escape(parent.post_shortcode,quote=True)}<br />',
                      f'Post URL: {html.escape(parent.post_url,quote=True)}<br />']
        return ImportPreview(parent=parent_dto(parent,children,recovery),projection_status=projection_status,
            expected_field_hashes=hashes,legacy_provenance_hashes=[field_hash(value) for value in provenance])
