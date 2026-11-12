"""Prepare local, analysis-only approvals. No SDK dispatch or production writes."""
from datetime import datetime,timedelta,timezone
import os,re
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from app.instagram_scraper.collection_review import build_queue
from app.instagram_scraper.collection_staging import digest,publication,window
from app.instagram_scraper.original_image_adapter import recover_original_images


def analyzer_input(media,profile):
    """Existing analyze_post input contract, preserving recovered original order.

    Ordering within this supplied single image/carousel is validated separately
    from Instagram source authenticity. This function NEVER establishes origin.
    """
    urls=recover_original_images(media).image_urls
    code=media.get('code');aliases=[media[key] for key in ('code','shortcode') if key in media]
    if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',code) or
            any(value!=code for value in aliases)):raise ValueError('Invalid shortcode identity')
    caption=media.get('caption')
    if not isinstance(caption,dict) or not isinstance(caption.get('text'),str) or not caption['text'].strip():
        raise ValueError('Missing usable caption')
    stamp=publication(media)
    if stamp is None:raise ValueError('Missing reliable publication timestamp')
    return dict(shortcode=code,permalink=f'https://www.instagram.com/p/{code}/',
        from_username=profile,title='',caption=caption['text'],taken_at=int(stamp.timestamp()),
        comments=[dict(kind='caption',text=caption['text'],username=profile)],
        openable_media_urls=dict(largest=list(urls),other=[]),first_image_url=urls[0],
        image_order_verified=True,source_authenticity_verified=False)


def paid_budget_snapshot(root):
    """Existing ledger's read-only summary; only allowlisted budget settings read."""
    from dotenv import dotenv_values
    from app.parent_pipeline.daily_budget import Ledger
    cfg=dotenv_values(Path(root).parent/'.env')
    def setting(name):return os.environ.get(name,cfg.get(name))
    try:
        limits={provider:int(setting(name) or 1) for provider,name in (
            ('gemini','INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS'),('openai_cover','INSTAGRAM_DAILY_COVER_MAX_REQUESTS'))}
        return Ledger(directory=setting('INSTAGRAM_PAID_REQUEST_LEDGER_DIR'),limits=limits).summary()
    except Exception:return dict(status='unavailable',reason='daily_paid_ledger_unavailable')


def prepare_batch(root,*,limit=5,now=None,journal_inspector=None,budget_snapshot=None,profiles=None,live_parent_lookup=None):
    if type(limit) is not int or not 1<=limit<=5:raise ValueError('Choose one to five candidates')
    root=Path(root);now=now or datetime.now(timezone.utc)
    if journal_inspector is None:
        from app.parent_pipeline.attempt_safety import inspect
        journal_inspector=inspect
    queue=build_queue(root,now);eligible=[];excluded={}
    def reject(reason):excluded[reason]=excluded.get(reason,0)+1
    for row in queue['records']:
        if profiles is not None and row['requested_profile'] not in profiles:continue
        media=json.loads((root/'objects'/(row['object']+'.json')).read_text())
        if digest(media)!=row['object']:raise ValueError('Staged object changed')
        if row['status']!='review_required':reject(row['status']);continue
        stamp=datetime.fromisoformat(row['publication_at']) if row['publication_at'] else None
        if stamp is None or not window(now)[1]<=stamp<=now+timedelta(minutes=5):reject('publication_not_recent_and_reliable');continue
        if not row['image']['passed']:reject('not_image_only');continue
        if row['screening']['status']!='eligible':reject('caption_date_not_confidently_future');continue
        if row['existing_imports'] or any(p.get('analyzed') for p in row['existing_parents']):reject('already_processed');continue
        if any(reason in row['reasons'] for reason in ('conflicting_identity_or_author','legacy_import_requires_explicit_adoption','canonical_import_conflict','conflicting_post_identity')):
            reject('identity_or_import_conflict');continue
        caption=media.get('caption',{}).get('text') if isinstance(media.get('caption'),dict) else None
        if not isinstance(caption,str) or not 40<=len(caption.strip())<=20000:reject('caption_too_short_or_invalid');continue
        post=analyzer_input(media,row['requested_profile'])
        # Prefer one actual-author observation of the SAME post over a discovery
        # through another account. This preference does NOT authenticate authorship.
        if live_parent_lookup is not None:
            if any(journal_inspector(p['parent_id'])['review_required'] for p in row['existing_parents']):
                reject('prior_paid_attempt_requires_separate_retry_approval');continue
            parents=live_parent_lookup(row['shortcode'])
            if any(p['analyzed'] or p['has_activities'] or p['has_import_ack'] for p in parents):
                reject('already_processed_authoritative');continue
            if len(parents)>1 or (parents and parents[0]['account']!=row['requested_profile']):
                reject('canonical_parent_conflict_requires_review');continue
            if parents and journal_inspector(parents[0]['parent_id'])['review_required']:
                reject('prior_paid_attempt_requires_separate_retry_approval');continue
            row=dict(row,existing_parents=[dict(parent_id=p['parent_id'],analyzed=p['analyzed']) for p in parents])
        rank=(row['requested_profile']==row['primary_author'],row['publication_at'])
        eligible.append((row,post,rank))
    unique={}
    for item in eligible:
        code=item[0]['shortcode']
        if code not in unique or item[2]>unique[code][2]:unique[code]=item
    duplicates=len(eligible)-len(unique)
    ordered=sorted(unique.values(),key=lambda item:item[0]['publication_at'],reverse=True)
    first=[];rest=[];accounts=set()
    for item in ordered:
        account=item[0]['requested_profile']
        (rest if account in accounts else first).append(item);accounts.add(account)
    selected=(first+rest)[:limit];records=[]
    for row,post,_ in selected:
        parents=row['existing_parents'];bound=parents[0]['parent_id'] if len(parents)==1 else None
        journal=journal_inspector(bound) if bound is not None else dict(status='parent_binding_required',review_required=True)
        if bound is not None and journal['review_required']:reject('prior_paid_attempt_requires_separate_retry_approval');continue
        record=dict(index=len(records)+1,account=row['requested_profile'],reported_primary_author=row['primary_author'],
            also_discovered_profiles=sorted({item[0]['requested_profile'] for item in eligible if item[0]['shortcode']==row['shortcode']}),
            shortcode=row['shortcode'],permalink=post['permalink'],publication_at=row['publication_at'],
            publication_los_angeles=datetime.fromisoformat(row['publication_at']).astimezone(ZoneInfo('America/Los_Angeles')).isoformat(),
            caption_date_candidates=row['screening']['dates'],caption_characters=len(post['caption']),
            original_image_count=len(post['openable_media_urls']['largest']),first_original_reference_available=True,
            image_bytes_fetched=False,image_url_accessibility_verified=False,
            deduplication='not_imported_or_analyzed_in_dated_snapshot; authoritative_recheck_required',
            parent_id=bound,journal=journal,staged_fingerprint=row['fingerprint'],source_object=row['object'],
            review_revision=row['revision'],analyzer_input_hash=digest(post),
            uncertainty_reasons=row['reasons'],source_verification=row['verification'],
            analysis_authorized=False,import_authorized=False,source_authenticity_verified=False,
            estimated_gemini_http_requests=1,max_http_requests_per_candidate=1,
            technical_handoff='existing_parent_analysis_only_adapter_required' if bound is not None else 'existing_journal_parent_binding_required')
        records.append(record)
    first_pilot=next((r for r in records if r['parent_id'] is not None and not r['journal']['review_required']),None)
    budget=paid_budget_snapshot(root) if budget_snapshot is None else budget_snapshot
    identity=dict(version=1,purpose='analysis_only_not_import',records=records)
    return dict(batch_id=digest(identity),prepared_at=now.isoformat(),status='prepared_not_authorized',
        candidates=records,selected_count=len(records),requested_maximum=limit,
        duplicate_observations_removed=duplicates,exclusion_counts=excluded,
        estimated_total_gemini_http_requests=len(records),proposed_first_run_global_http_cap=1,
        recommended_first_candidate=first_pilot['index'] if first_pilot else None,
        daily_budget=budget,actual_gemini_requests=0,actual_openai_requests=0,production_database_writes=0,
        covers_authorized=False,imports_authorized=False,scheduling_enabled=False,
        authorization_scope='Explicit analysis-only approval does not grant source authenticity or import permission',
        analyzer='app.ai_analyze_service.gemini_analysis.analyze_post',production_handoff='disabled')
