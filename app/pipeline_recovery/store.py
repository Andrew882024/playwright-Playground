"""Short PostgreSQL transactions only. Network work never holds a state lock."""
from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy import select, or_, text
from sqlalchemy.dialects.postgresql import insert
from app.instagram_post_presentation import import_source_revision, canonical_cover_inputs
from app.event_cover_service.openai_cover import digest, json_bytes


def revision(stage, parent, children):
    if stage == 'event_master':
        return import_source_revision(parent, children)
    return digest(json_bytes(dict(inputs=canonical_cover_inputs(parent, children),
        source=[parent.own_s3_url_for_main_image or '', parent.main_image_url or ''])))


def blocked(state, rev, now, max_failures):
    if state is None:
        return None
    if state.lease_expires_at and state.lease_expires_at > now:
        return 'leased'
    if state.status == 'review_required':
        return 'review_required'
    if state.source_revision == rev:
        if state.status == 'succeeded' and state.acknowledged_revision == rev:
            return 'already_current'
        if state.failure_count >= max_failures:
            return 'retry_limit'
        if state.next_attempt_at and state.next_attempt_at > now:
            return 'cooldown'
    return None


class Store:
    def __init__(self, factory, cutoff, max_failures=5):
        self.factory, self.cutoff, self.max_failures = factory, cutoff, max_failures

    def snapshot(self, session, parent_id, lock=False):
        from Blueprint_db import InstagramPosts as P, InstagramEvents as E
        from app.ai_analyze_service.candidates import posted_at_utc_coalesce
        stmt = select(P).where(P.id == parent_id, P.ai_analyzed.is_(True),
                              posted_at_utc_coalesce() >= self.cutoff)
        if lock: stmt = stmt.with_for_update()
        parent = session.execute(stmt).scalar_one_or_none()
        if parent is None: return None
        children = list(session.scalars(select(E).where(E.instagram_post_id == parent_id).order_by(E.id)
                         .with_for_update() if lock else select(E).where(E.instagram_post_id == parent_id).order_by(E.id)).all())
        return (parent, children) if children else None

    def candidates(self, stage, excluded, limit=100, preview=False):
        from Blueprint_db import InstagramPosts as P, InstagramEvents as E, InstagramPipelineRecovery as R
        from app.ai_analyze_service.candidates import posted_at_utc_coalesce
        now = datetime.now(timezone.utc)
        with self.factory() as s:
            if preview: s.execute(text('SET TRANSACTION READ ONLY'))
            # SQL filters keep completed/cooling/review rows from starving later work.
            stmt = select(P.id).outerjoin(R, (R.instagram_post_id == P.id) & (R.stage == stage)).where(
                P.ai_analyzed.is_(True), posted_at_utc_coalesce() >= self.cutoff,
                select(E.id).where(E.instagram_post_id == P.id).exists(),
                P.id.not_in(excluded),
                or_(R.lease_expires_at.is_(None), R.lease_expires_at <= now))
            if stage == 'cover':
                from sqlalchemy import func
                stmt = stmt.where(func.nullif(func.trim(P.generated_image_url), '').is_(None))
            if not preview:
                stmt = stmt.where(or_(R.status.is_(None), R.status != 'review_required'))
            # Rotate bounded scans; hashes, not timestamps, decide import freshness.
            stmt = stmt.where(or_(R.next_attempt_at.is_(None), R.next_attempt_at <= now,
                                  P.updated_at > R.last_attempt_at))
            stmt = stmt.order_by(R.last_checked_at.asc().nulls_first(), R.last_attempt_at.asc().nulls_first(), P.id).limit(limit)
            ids = list(s.scalars(stmt).all())
            result=[]
            for parent_id in ids:
                snap=self.snapshot(s,parent_id)
                if snap is None: continue
                parent,children=snap
                rev=revision(stage,parent,children)
                state=s.get(R,(parent_id,stage))
                if state is not None and not preview: state.last_checked_at=now
                reason=blocked(state,rev,now,self.max_failures)
                if stage=='cover' and reason=='already_current': reason=None
                if stage=='cover' and not ((parent.own_s3_url_for_main_image or '').strip() or
                                           (parent.main_image_url or '').strip()): reason='missing_source'
                result.append(dict(parent_id=parent_id,reason=reason or 'incomplete',
                    expected_action=('resume_or_generate_cover' if stage=='cover' else 'idempotent_parent_sync')))
            if not preview: s.commit()  # Scan cursor only; never acknowledges processing.
            return result

    def claim(self, stage, parent_id):
        from Blueprint_db import InstagramPipelineRecovery as R
        now=datetime.now(timezone.utc)
        with self.factory() as s, s.begin():
            snap=self.snapshot(s,parent_id,lock=True)
            if snap is None: return None
            parent,children=snap
            rev=revision(stage,parent,children)
            # Fixed lock order: parent, children, recovery row.
            s.execute(insert(R).values(instagram_post_id=parent_id,stage=stage,source_revision=rev,
                status='pending',attempt_count=0,failure_count=0).on_conflict_do_nothing())
            state=s.execute(select(R).where(R.instagram_post_id==parent_id,R.stage==stage).with_for_update()).scalar_one()
            reason=blocked(state,rev,now,self.max_failures)
            if reason and not (stage=='cover' and reason=='already_current'): return None
            if stage=='cover' and (parent.generated_image_url or '').strip(): return None
            if state.source_revision != rev: state.failure_count=0
            token=uuid.uuid4().hex
            state.source_revision=rev;state.status='pending';state.lease_token=token
            state.lease_expires_at=now+timedelta(hours=1);state.last_attempt_at=now
            state.attempt_count+=1
            return dict(parent_id=parent_id,stage=stage,token=token,revision=rev)

    def finish(self, claim, status, reason, acknowledged=None):
        from Blueprint_db import InstagramPipelineRecovery as R
        now=datetime.now(timezone.utc)
        with self.factory() as s, s.begin():
            snap=self.snapshot(s,claim['parent_id'],lock=True)
            state=s.execute(select(R).where(R.instagram_post_id==claim['parent_id'],R.stage==claim['stage']).with_for_update()).scalar_one()
            if state.lease_token != claim['token']: return 'lease_lost'
            state.lease_token=None;state.lease_expires_at=None
            current=revision(claim['stage'],*snap) if snap else None
            # Cover publication itself changes URL, but not the cover source revision.
            if status=='succeeded' and (current != claim['revision'] or
                    (claim['stage']=='event_master' and acknowledged != current)):
                status,reason='failed','stale_revision'
            state.status=status;state.safe_reason=reason
            if status=='succeeded':
                state.acknowledged_revision=current;state.failure_count=0;state.next_attempt_at=None
            elif status=='pending':
                state.next_attempt_at=now+timedelta(minutes=15)
            elif status=='failed':
                state.failure_count+=1
                state.next_attempt_at=now+timedelta(minutes=min(1440,15*2**min(state.failure_count-1,7)))
                if state.failure_count >= self.max_failures: state.status='review_required';state.safe_reason='retry_limit'
            return state.status
