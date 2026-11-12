"""Revision-bound analysis-only parent binding. No paid dispatch or trust override."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from app.instagram_scraper.analysis_batch import analyzer_input
from app.instagram_scraper.analysis_execution import Blocked, authoritative_state
from app.instagram_scraper.collection_staging import atomic, digest, private_dir, window
from app.parent_pipeline import attempt_safety


REVIEW_KEY = 'analysis_review'


def production_candidates_only(model):
    """Exclude quarantined review parents from ALL normal analyzer selection."""
    from sqlalchemy import func
    # Presence blocks even corrupt/null/false review metadata; no implicit release.
    return ~func.coalesce(model.comments_json.has_key(REVIEW_KEY), False)


def load_candidate(root, batch_id, index):
    if not re.fullmatch('[a-f0-9]{64}', batch_id) or type(index) is not int or index < 1:
        raise Blocked('invalid_batch_or_candidate')
    path = Path(root)/'analysis-batches'/(batch_id+'.json')
    if path.is_symlink():raise Blocked('unsafe_manifest')
    batch = json.loads(path.read_text())
    if (batch.get('batch_id') != batch_id or
            digest(dict(version=1, purpose='analysis_only_not_import', records=batch['candidates'])) != batch_id):
        raise Blocked('batch_revision_changed')
    matches = [r for r in batch['candidates'] if r['index'] == index]
    if len(matches) != 1:raise Blocked('candidate_missing_or_ambiguous')
    return load_record(root, matches[0])


def load_record(root, row):
    """Same immutable-input/screening checks for in-memory batch previews."""
    object_id = row['source_object']
    if not re.fullmatch('[a-f0-9]{64}', object_id):raise Blocked('invalid_source_object')
    path = Path(root)/'objects'/(object_id+'.json')
    if path.is_symlink():raise Blocked('unsafe_source_object')
    media = json.loads(path.read_text())
    if digest(media) != object_id:raise Blocked('staged_object_changed')
    post = analyzer_input(media, row['account'])
    if digest(post) != row['analyzer_input_hash'] or post['shortcode'] != row['shortcode']:
        raise Blocked('analysis_input_changed')
    # A collaborative discovery cannot silently allocate a parent to the wrong
    # account. Such observations require an independently established binding.
    if row['reported_primary_author'] != row['account']:
        raise Blocked('primary_author_binding_requires_review')
    from app.parent_pipeline.screening import screen
    now = datetime.now(timezone.utc)
    if not window(now)[1].timestamp() <= post['taken_at'] <= now.timestamp()+300:
        raise Blocked('outside_recent_window')
    if screen(dict(caption=post['caption'], posted_unix_seconds=post['taken_at']), now)['status'] != 'eligible':
        raise Blocked('event_date_no_longer_eligible')
    from app.instagram_scraper.collection_review import canonical_index
    imports, _ = canonical_index(root)
    local = Path(root)/'canonical_imports.json'
    index_data = json.loads(local.read_text()) if local.exists() else {}
    if any(r['shortcode'] == row['shortcode'] for r in imports):raise Blocked('already_imported')
    if row['shortcode'] in index_data.get('legacy_shortcodes', []):raise Blocked('legacy_import_requires_review')
    return row, post


def lookup_parents(shortcode):
    """Authoritative SELECTs only, across all accounts and all historical rows."""
    from sqlalchemy import select, text
    from Blueprint_db import SessionLocal, InstagramPosts as P, InstagramEvents as E, InstagramPipelineRecovery as R
    with SessionLocal(autoflush=False) as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        parents = list(db.scalars(select(P).where(P.post_shortcode == shortcode)).all())
        result = []
        for p in parents:
            children = db.scalar(select(E.id).where(E.instagram_post_id == p.id).limit(1)) is not None
            ack = db.get(R, (p.id, 'event_master'))
            unix = p.posted_unix_seconds
            if p.posted_time is not None:
                if p.posted_time.utcoffset() is None:raise Blocked('canonical_publication_unreliable')
                from_time = int(p.posted_time.timestamp())
                if unix is not None and unix != from_time:raise Blocked('canonical_publication_conflict')
                unix = from_time
            result.append(dict(parent_id=p.id, account=p.profile_username, analyzed=bool(p.ai_analyzed),
                has_activities=children, has_import_ack=bool(ack and ack.acknowledged_revision),
                caption=p.caption, taken_at=unix))
        return result


def check_binding(row, post, parents):
    if not parents:
        if row.get("parent_id") is not None:raise Blocked("canonical_parent_binding_missing")
        return None
    if len(parents) != 1:raise Blocked('canonical_parent_ambiguous')
    found = parents[0];parent_id = found['parent_id']
    if found['account'] != row['account']:raise Blocked('canonical_author_conflict')
    if found['analyzed'] or found['has_activities']:raise Blocked('already_analyzed_or_extracted')
    if found['has_import_ack']:raise Blocked('already_imported_acknowledgment')
    if found['caption'] != post['caption'] or found['taken_at'] != post['taken_at']:
        raise Blocked('canonical_input_revision_conflict')
    if row.get('parent_id') is not None and row['parent_id'] != parent_id:
        raise Blocked('canonical_parent_binding_changed')
    if attempt_safety.inspect(parent_id)['review_required']:
        raise Blocked('previous_analysis_outcome_requires_review')
    return parent_id


def review_metadata(row, source_batch):
    # Stored source uncertainties, never a production verification attestation.
    return dict(version=1, scope='local_analysis_only', production_eligible=False,
        source_authenticity_verified=False, import_authorized=False,
        source_batch=source_batch, source_object=row['source_object'],
        staged_fingerprint=row['staged_fingerprint'], review_revision=row['review_revision'],
        analyzer_input_hash=row['analyzer_input_hash'],
        requested_profile=row['account'], reported_primary_author=row['reported_primary_author'],
        discovered_profiles=row['also_discovered_profiles'],
        uncertainty_reasons=row['uncertainty_reasons'], source_verification=row['source_verification'])


def bind_candidate(root, batch_id, index, *, write=False):
    row, post = load_candidate(root, batch_id, index)
    found = lookup_parents(row['shortcode']);parent_id = check_binding(row, post, found)
    result = binding_summary(row, index, parent_id)
    if not write:return result
    # Explicit invocation approves classification of untrusted source material,
    # NOT production source authenticity. No old manifest or journal is rewritten.
    from app.parent_pipeline.locks import advisory_lock
    key = int(digest(row['shortcode'])[:8], 16) & 0x7fffffff
    with advisory_lock(1701669010, key) as held:
        if not held:raise Blocked('canonical_binding_busy')
        # Recheck immutable inputs and live state after acquiring canonical lock.
        row, post = load_candidate(root, batch_id, index)
        found = lookup_parents(row['shortcode']);parent_id = check_binding(row, post, found)
        if parent_id is None:
            from app.instagram_scraper.scrape_db import insert_analysis_parent
            parent_id, inserted = insert_analysis_parent(row['account'], post, review_metadata(row, batch_id))
            result['database_writes'] = int(inserted)
        # Never guess an ID or silently choose a second author's parent.
        after = lookup_parents(row['shortcode'])
        if check_binding(row, post, after) != parent_id:raise Blocked('canonical_binding_conflict')
        authoritative_state(parent_id, row, post)
        record = deepcopy(row)
        record.update(index=1, parent_id=parent_id, analysis_authorized=True,
            approval_scope='local_analysis_only_untrusted_source',
            journal=attempt_safety.inspect(parent_id), technical_handoff='analysis_only_executor',
            import_authorized=False, source_authenticity_verified=False)
        identity = dict(version=1, purpose='analysis_only_not_import', records=[record])
        new_id = digest(identity)
        manifest = dict(batch_id=new_id, prepared_at=datetime.now(timezone.utc).isoformat(),
            status='approved_analysis_only', candidates=[record], source_batch=batch_id,
            selected_count=1, proposed_first_run_global_http_cap=1,
            actual_gemini_requests=0, actual_openai_requests=0, imports_authorized=False)
        directory = private_dir(Path(root)/'analysis-batches')
        atomic(directory/(new_id+'.json'), manifest)
        result.update(status='bound_analysis_only', parent_id=parent_id, analysis_batch_id=new_id,
            next_command=f'./venv/bin/python analyze_instagram_batch.py --batch-id {new_id} --parent-id {parent_id} --preflight',
            analysis_command=f'./venv/bin/python analyze_instagram_batch.py --batch-id {new_id} --parent-id {parent_id} --run --confirm-one-paid-request')
    return result


def binding_summary(row, index, parent_id):
    return dict(account=row['account'], shortcode=row['shortcode'], permalink=row['permalink'],
        candidate=index, parent_id=parent_id, status='existing_parent' if parent_id else 'creation_required',
        source_authenticity_verified=False, import_authorized=False,
        source_uncertainties=row['uncertainty_reasons'], database_writes=0, paid_requests=0,
        mysql_import_absence_verified=False)


def preview_record(root, row):
    """Read-only preview without creating a temporary manifest or parent."""
    row, post = load_record(root, row)
    parent_id = check_binding(row, post, lookup_parents(row['shortcode']))
    return binding_summary(row, row['index'], parent_id)
