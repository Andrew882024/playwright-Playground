"""Local multi-account review queue. No paid/production handoff is enabled."""
from collections import Counter
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import re
from app.instagram_collaboration import normalize_collaboration
from app.instagram_scraper.original_image_adapter import recover_original_images,ImageRecoveryError
from app.instagram_scraper.media_identity import inspect_identity,normalize_identity
from app.instagram_scraper.permalink_verification import _normalize_post_fields,Unverified
from app.parent_pipeline.screening import screen


def configured_accounts():
    from app.instagram_scraper.tempdata import TARGET_URLS
    names=list(dict.fromkeys(TARGET_URLS))
    if not names or len(names)>16 or any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9._]{1,30}',name) for name in names):
        raise ValueError('Invalid configured account list')
    return names


def inspect_candidate(media,profile,now,*,has_errors=True,conflict=False,imports=(),processed=(),legacy=()):
    # Content/identity uncertainty is a review attribute, never a collection veto.
    from app.instagram_scraper.collection_staging import publication,window
    meta=normalize_collaboration(media,profile);stamp=publication(media)
    reasons={'source_verification_unavailable','authentication_attestation_missing','production_handoff_disabled'}
    if has_errors:reasons.add('graphql_errors_unresolved_for_post')
    if meta.primary_author!=profile:reasons.add('accepted_collaboration_unverified')
    reasons.update(meta.review_reasons)
    if conflict:reasons.add('conflicting_identity_or_author')
    identity=False
    try:normalize_identity(media);identity=True
    except ValueError as exc:reasons.add(str(exc))
    try:_normalize_post_fields(media)
    except Unverified as exc:reasons.add(str(exc))
    image=dict(passed=False,image_count=0,layout='unknown_media')
    try:
        recovered=recover_original_images(media)
        image=dict(passed=True,image_count=len(recovered.image_urls),layout=recovered.layout,
                   variant_count=recovered.variant_count,order_basis=recovered.original_order_basis)
        if recovered.layout=='image_only_carousel':
            children=media.get('carousel_media')
            if children is None:children=[e['node'] for e in media['edge_sidecar_to_children']['edges']]
            try:
                seen={normalize_identity(media)}
                for child in children:
                    ident=normalize_identity(child)
                    if ident in seen:raise ValueError('duplicate_carousel_child')
                    seen.add(ident)
            except ValueError as exc:identity=False;reasons.add(str(exc))
    except ImageRecoveryError as exc:reasons.add(str(exc));image['reason']=str(exc)
    except Exception:reasons.add('image_validation_failed')
    caption=media.get('caption');text=caption.get('text') if isinstance(caption,dict) else None
    screening=screen(dict(caption=text,posted_time=stamp),now)
    if screening['status']!='eligible':reasons.add('event_screening_'+screening['status'])
    matches=[item for item in imports if item.get('shortcode')==meta.post_shortcode]
    canonical='already_imported' if len(matches)==1 else 'conflict' if matches else 'not_verified'
    if canonical=='conflict':reasons.add('canonical_import_conflict')
    if canonical=='not_verified':reasons.add('canonical_import_check_required')
    recent=stamp is not None and window(now)[1]<=stamp<=now+timedelta(minutes=5)
    if not recent:reasons.add('publication_date_outside_window_or_unknown')
    existing=[item for item in processed if item.get('shortcode')==meta.post_shortcode]
    if meta.post_shortcode in legacy:
        canonical='legacy_conflict';reasons.add('legacy_import_requires_explicit_adoption');status='review_required'
    elif conflict:status='review_required'
    elif matches:status='skipped_already_imported' if len(matches)==1 else 'review_required'
    elif existing and any(item.get('analyzed') for item in existing):
        status='skipped_already_analyzed';reasons.add('reuse_existing_parent_for_downstream_work')
    elif not recent:status='review_required' if stamp is None else 'skipped_outside_window'
    elif not image['passed']:status='skipped_media'
    elif screening['status']=='skipped_expired':status='skipped_expired'
    else:status='review_required'
    return dict(status=status,reasons=sorted(reasons),image=image,screening=screening,
        primary_author=meta.primary_author,requested_profile=profile,shortcode=meta.post_shortcode,
        publication_at=stamp.isoformat() if stamp else None,canonical_status=canonical,
        existing_imports=matches,existing_parents=existing,identity=inspect_identity(media),
        verification=dict(media_identity=identity,source_membership=False,authentication=False,
                          collaboration=False,graphql_integrity=False,canonical_checked=bool(matches)),
        explicit_approval=False,ready_for_paid=False,ready_for_import=False)


def canonical_index(root):
    path=Path(root)/'canonical_imports.json'
    if not path.exists():return [],'not_available'
    if path.is_symlink():raise ValueError('Unsafe canonical snapshot')
    data=json.loads(path.read_text())
    if data.get('version')!=1 or not isinstance(data.get('imports'),list):raise ValueError('Invalid canonical snapshot')
    from app.instagram_event_master_projection import parent_marker
    records=[]
    for item in data['imports']:
        if (not isinstance(item,dict) or type(item.get('parent_id')) is not int or item['parent_id']<=0 or
                type(item.get('event_id')) is not int or item['event_id']<=0 or
                not isinstance(item.get('shortcode'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',item['shortcode']) or
                item.get('marker')!=parent_marker(item['parent_id'])):raise ValueError('Invalid canonical mapping')
        records.append(item)
    # An index is a NEGATIVE spending guard only. Absence is never proof that an
    # event is not imported. Fresh authoritative recheck is required before payment.
    return records,data.get('captured_at','unknown')


def build_queue(root,now=None):
    from app.instagram_scraper.collection_staging import digest,window
    root=Path(root);now=now or datetime.now(timezone.utc);imports,captured=canonical_index(root)
    if not (root/'state.json').exists():return dict(status='empty',accounts=configured_accounts(),records=[],counts={},ready_for_import=0)
    state=json.loads((root/'state.json').read_text());counts=Counter();rows=[];per_account={}
    index=json.loads((root/'canonical_imports.json').read_text()) if (root/'canonical_imports.json').exists() else {}
    for fingerprint,record in state['records'].items():
        if record.get('publication_at') and datetime.fromisoformat(record['publication_at'])<window(now)[1]:continue
        profiles=record.get('discovered_profiles') or ['dvcatucsd']  # legacy v1 staging
        for profile in profiles:
            observation=record.get('observations_by_profile',{}).get(profile,{})
            object_id=observation.get('object',record['latest_object'])
            media=json.loads((root/'objects'/(object_id+'.json')).read_text())
            provenance=observation.get('provenance',record.get('provenance_by_profile',{}).get(profile,{}))
            # Legacy snapshots without response provenance fail closed.
            decision=inspect_candidate(media,profile,now,has_errors=provenance.get('graphql_errors',True),
                conflict=record.get('conflict',False),imports=imports,processed=index.get('processed',()),legacy=index.get('legacy_shortcodes',()))
            revision=digest(dict(fingerprint=fingerprint,object=object_id,profile=profile,decision=decision))
            row=dict(fingerprint=fingerprint,object=object_id,revision=revision,**decision)
            review_path=root/'reviews'/(digest([fingerprint,profile])+'.json')
            if review_path.exists():
                request=json.loads(review_path.read_text())
                row['approval_request_status']='pending_verified_evidence' if request.get('revision')==revision else 'stale_review'
            counts[decision['status']]+=1;account=per_account.setdefault(profile,Counter())
            account['collected']+=1;account[decision['status']]+=1
            account['image_only']+=decision['image']['passed'];counts['image_only']+=decision['image']['passed']
            rows.append(row)
    return dict(status='review_only',timezone='America/Los_Angeles',cutoff=window(now)[1].isoformat(),
                canonical_snapshot=captured,accounts=configured_accounts(),per_account={k:dict(v) for k,v in per_account.items()},
                counts=dict(counts),records=rows,ready_for_paid=0,ready_for_import=0,
                gemini_requests=0,openai_requests=0,database_writes=0,production_handoff='disabled')


def record_review(root,fingerprint,profile,revision):
    # Explicit intent is durable/revision-bound, not an evidence override. No
    # flag can grant missing source/identity/integrity proof or dispatch payment.
    from app.instagram_scraper.collection_staging import atomic,private_dir,digest
    from app.instagram_scraper.daily_pilot import single_run_lock
    if not re.fullmatch(r'[0-9a-f]{64}',fingerprint) or not re.fullmatch(r'[0-9a-f]{64}',revision):raise ValueError('Invalid revision')
    with single_run_lock(root) as held:
        if not held:return dict(status='busy')
        matches=[r for r in build_queue(root)['records'] if r['fingerprint']==fingerprint and r['requested_profile']==profile]
        if len(matches)!=1 or matches[0]['revision']!=revision:return dict(status='rejected',reason='stale_or_missing_review')
        row=matches[0]
        if row['status']!='review_required':return dict(status='rejected',reason='record_not_reviewable')
        directory=private_dir(Path(root)/'reviews')
        atomic(directory/(digest([fingerprint,profile])+'.json'),dict(fingerprint=fingerprint,profile=profile,
            revision=revision,review_requested_at=datetime.now(timezone.utc).isoformat(),explicit_review_request=True,
            evidence_sufficient=False,paid_authorized=False,import_authorized=False,reasons=row['reasons']))
        return dict(status='review_recorded',reason='verified_evidence_and_execution_approval_still_required',ready_for_paid=False,ready_for_import=False)
