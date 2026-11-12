"""Private local candidate staging, never a production/AI eligibility handoff."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from app.instagram_collaboration import normalize_collaboration
from app.instagram_scraper.graphql_diagnostics import TIMELINE_ROOTS, classify_error_path
from app.instagram_scraper.recency import OldPostStopper
from app.instagram_scraper.media_identity import normalize_identity, numeric_id
from app.instagram_scraper.permalink_verification import _kind, _image_reference, _normalize_post_fields, Unverified
from app.parent_pipeline.screening import screen

ZONE=ZoneInfo('America/Los_Angeles')


class CollectionStop(BaseException):
    pass


def utc(value):
    if not isinstance(value,datetime) or value.utcoffset() is None:raise ValueError('Aware clock required')
    return value.astimezone(timezone.utc)


def window(now):
    # A rolling 40*24-hour publication window, anchored to the LA run instant.
    local=utc(now).astimezone(ZONE)
    return local,utc(local)-timedelta(days=40)


def publication(media):
    values=[media[key] for key in ('taken_at','taken_at_timestamp','device_timestamp') if key in media]
    if not values or any(type(value) is not int or value<=0 for value in values) or len(set(values))!=1:return None
    try:return datetime.fromtimestamp(values[0],timezone.utc)
    except (ValueError,OverflowError,OSError):return None


def _video(media):
    return (media.get('media_type')==2 or media.get('__typename')=='GraphVideo' or media.get('is_video') is True or
            media.get('product_type') in ('clips','reels') or bool(media.get('video_url')) or bool(media.get('video_versions')))


def media_class(media):
    if _video(media):return 'video_or_reel'
    try:kind=_kind(media)
    except Unverified:return 'unknown_media'
    children=media.get('carousel_media')
    if children is None and isinstance(media.get('edge_sidecar_to_children'),dict):
        connection=media['edge_sidecar_to_children'];edges=connection.get('edges')
        if not isinstance(edges,list) or connection.get('page_info',{}).get('has_next_page') is not False:return 'unknown_media'
        children=[item.get('node') if isinstance(item,dict) else None for item in edges]
        count=connection.get('count')
    else:count=media.get('carousel_media_count')
    if kind==1:
        if children is not None:return 'unknown_media'
        return 'single_image_fields' if _image_reference(media) else 'unknown_media'
    if not isinstance(children,list) or not 1<=len(children)<=20:return 'unknown_media'
    if any(isinstance(child,dict) and _video(child) for child in children):return 'mixed_media_carousel'
    if type(count) is not int or count!=len(children):return 'unknown_media'
    try:
        if all(isinstance(child,dict) and _kind(child)==1 and _image_reference(child) for child in children):
            return 'image_only_carousel_fields'
    except Unverified:pass
    return 'unknown_media'


def private_dir(path):
    path=Path(path);path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.stat()
    if path.is_symlink() or info.st_uid!=os.getuid() or info.st_mode & 0o077:raise ValueError('Private staging directory required')
    return path


def atomic(path,value):
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.write-')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def clean_media(value,depth=0,budget=None):
    budget=[0] if budget is None else budget;budget[0]+=1
    if depth>16 or budget[0]>10000:raise CollectionStop('metadata_limit')
    if isinstance(value,dict):
        return {key:clean_media(item,depth+1,budget) for key,item in value.items() if isinstance(key,str) and
                not re.search(r'cookie|password|authorization|token|sessionid|request_body|headers',key,re.I)}
    if isinstance(value,list):return [clean_media(item,depth+1,budget) for item in value]
    if value is None or type(value) in (str,int,float,bool):return value
    raise CollectionStop('metadata_type_unsupported')


def _fingerprint(value):
    if isinstance(value,dict):return {key:_fingerprint(item) for key,item in value.items()}
    if isinstance(value,list):return [_fingerprint(item) for item in value]
    if isinstance(value,str) and value.startswith('https://'):
        try:
            url=urlsplit(value);return urlunsplit((url.scheme,url.netloc,url.path,'',''))
        except ValueError:pass
    return value


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class StagingCollector:
    def __init__(self,root,run_id,mode,now,profile="dvcatucsd"):
        if not isinstance(profile,str) or not re.fullmatch(r"[A-Za-z0-9._]{1,30}",profile):raise ValueError("Invalid profile")
        self.profile=profile
        self.root=private_dir(root);self.objects=private_dir(self.root/'objects')
        self.runs=private_dir(self.root/'runs');self.run_id=run_id;self.mode=mode;self.now=utc(now)
        self.local,self.cutoff=window(now);self.counts=Counter();self.reasons=Counter()
        self.recent=set();self.unknown=set();self.media_counts=Counter();self.request_diagnostics={}
        self.response_number=0;self.stopper=None;self.boundary_reached=False;self.new_bytes=0;self.failure=None
        state=self.root/'state.json'
        if state.is_symlink():raise ValueError('Unsafe staging state')
        self.state=json.loads(state.read_text()) if state.exists() else dict(version=1,records={},last_collection_day=None)
        if self.state.get('version')!=1 or not isinstance(self.state.get('records'),dict):raise ValueError('Invalid staging state')
        self.state.setdefault('collection_days',{})
        if self.state.get('last_collection_day') and 'dvcatucsd' not in self.state['collection_days']:
            self.state['collection_days']['dvcatucsd']=self.state['last_collection_day']
        self.checkpoint('running','collection_started')

    def observe(self,payload,category):
        # Production capture catches Exception. Never let a staging error be
        # swallowed and subsequently reported as a successful collection.
        try:self._observe(payload,category)
        except CollectionStop:raise
        except Exception:
            self.failure='staging_processing_failed'
            raise CollectionStop(self.failure) from None

    def _observe(self,payload,category):
        self.response_number+=1
        root=payload.get('data') if isinstance(payload,dict) else None
        if not isinstance(root,dict):self.reasons['unsupported_response_structure']+=1;return
        remaining=128
        for name,connection in list(root.items())[:64]:
            if name not in TIMELINE_ROOTS or not isinstance(connection,dict) or not isinstance(connection.get('edges'),list):continue
            for index,edge in enumerate(connection['edges'][:remaining]):
                remaining-=1
                if not isinstance(edge,dict) or not isinstance(edge.get('node'),dict):
                    self.counts['null_or_malformed_slots']+=1;continue
                node=edge['node'];wrapper='flat_node'
                if 'code' in node:media=node
                elif 'media' in node:
                    media=node['media'];wrapper='node_media'
                    if media is None:self.counts['null_or_malformed_slots']+=1;continue
                else:self.counts['unsupported_slots']+=1;continue
                if not isinstance(media,dict):self.counts['null_or_malformed_slots']+=1;continue
                stamp=publication(media)
                if stamp is not None and stamp<self.cutoff:
                    self.counts['older_observations_excluded']+=1;continue
                unknown=stamp is None or stamp>self.now+timedelta(minutes=5)
                if unknown:self.counts['unknown_timestamp_observations']+=1
                else:self.counts['recent_observations']+=1
                self.stage(media,stamp,unknown,dict(root=name,wrapper=wrapper,node_index=index,
                    response_number=self.response_number,response_category=category,
                    graphql_errors=bool(payload.get('errors')),error_categories=dict(Counter(
                        classify_error_path(item.get('path'))['category'] if isinstance(item,dict) else 'malformed_path'
                        for item in (payload.get('errors') or [])[:128])) if isinstance(payload.get('errors',[]),list) else {'malformed_errors':1},
                    request_diagnostics=self.request_diagnostics))
        # No current response-to-profile evidence can establish chronological
        # exhaustion. Never stop merely because these unverified batches are old.
        self.checkpoint('running','collecting_bounded_window')

    def chronological_batch(self,posts,*,source_order_verified=False):
        """Existing pinned-aware rule, only for independently verified ordering."""
        if source_order_verified is not True:return False
        if self.stopper is None:
            self.stopper=OldPostStopper([post['shortcode'] for post in posts],self.cutoff);return False
        self.boundary_reached=self.stopper.observe(posts)
        return self.boundary_reached

    def stage(self,media,stamp,unknown,provenance):
        cleaned=clean_media(media)
        encoded=json.dumps(cleaned,sort_keys=True,separators=(',',':')).encode()
        if len(encoded)>2*1024*1024:raise CollectionStop('metadata_size_limit')
        metadata=normalize_collaboration(media,self.profile)
        try:canonical=normalize_identity(media)
        except ValueError:canonical=None
        identity_verified=bool(canonical and metadata.post_shortcode and metadata.primary_author and
            ('shortcode' not in media or media['shortcode']==metadata.post_shortcode) and
            'author_owner_conflict' not in metadata.review_reasons)
        signature=digest(dict(code=metadata.post_shortcode,
            pk=numeric_id(media.get('pk')) or media.get('pk'),
            id=numeric_id(media.get('id')) or media.get('id'),author=metadata.primary_author))
        object_id=digest(cleaned)
        fingerprint=(digest(dict(code=metadata.post_shortcode,media_id=canonical,author=metadata.primary_author))
                     if identity_verified else signature if metadata.post_shortcode and metadata.primary_author and
                     ('pk' in media or 'id' in media) else digest(_fingerprint(cleaned)))
        records=self.state['records']
        if len(records)>=5000 and fingerprint not in records:raise CollectionStop('staging_record_limit')
        if fingerprint in records:self.counts['duplicate_observations']+=1
        else:self.counts['new_candidate_fingerprints']+=1
        object_path=self.objects/(object_id+'.json')
        if not object_path.exists():
            self.new_bytes+=len(encoded)
            if self.new_bytes>64*1024*1024:raise CollectionStop('staging_storage_limit')
            atomic(object_path,cleaned)
        blockers={'source_verification_unavailable','production_handoff_disabled'}
        if metadata.primary_author!=self.profile:blockers.add('accepted_collaboration_unverified')
        if provenance['graphql_errors']:blockers.add('graphql_errors_unresolved_for_post')
        try:canonical=normalize_identity(media)
        except ValueError as exc:canonical=None;blockers.add(str(exc))
        from app.instagram_scraper.collection_review import inspect_candidate,canonical_index
        imports,_=canonical_index(self.root)
        index=json.loads((self.root/'canonical_imports.json').read_text()) if (self.root/'canonical_imports.json').exists() else {}
        review=inspect_candidate(media,self.profile,self.now,has_errors=provenance['graphql_errors'],imports=imports,
                                 processed=index.get('processed',()),legacy=index.get('legacy_shortcodes',()))
        classification={'single_image':'single_image_fields','image_only_carousel':'image_only_carousel_fields'}.get(review['image']['layout'],media_class(media))
        if classification not in ('single_image_fields','image_only_carousel_fields'):blockers.add(classification)
        try:_normalize_post_fields(media)
        except Unverified as exc:blockers.add(str(exc))
        if unknown:blockers.add('publication_timestamp_unverified')
        else:
            caption=media.get('caption')
            screening=screen(dict(caption=caption.get('text') if isinstance(caption,dict) else None,posted_time=stamp),self.now)
            if screening['status']!='eligible':blockers.add('event_screening_'+screening['status'])
        conflict=False
        for previous in records.values():
            if metadata.post_shortcode and previous['shortcode']==metadata.post_shortcode and previous['identity_signature']!=signature:
                previous['conflict']=True;conflict=True
        old=records.get(fingerprint,{})
        record=dict(shortcode=metadata.post_shortcode,primary_author=metadata.primary_author,
            canonical_media_id=canonical,identity_verified=identity_verified,
            deduplication='canonical_identity' if identity_verified else 'exact_unverified_identity_tuple',
            identity_signature=signature,conflict=conflict or old.get('conflict',False),
            publication_at=stamp.isoformat() if stamp else None,unknown_timestamp=unknown,
            media_class=classification,latest_object=object_id,ai_eligible=False,source_membership_verified=False,
            first_seen_run=old.get('first_seen_run',self.run_id),last_seen_run=self.run_id,
            verification_blockers=sorted(blockers),
            discovered_profiles=sorted(set(old.get('discovered_profiles', ['dvcatucsd'] if old else []))|{self.profile}),
            provenance_by_profile={**old.get('provenance_by_profile',{}),self.profile:provenance},
            observations_by_profile={**old.get('observations_by_profile',{}),self.profile:dict(object=object_id,provenance=provenance)},
            review_status=review['status'],review=review)
        records[fingerprint]=record
        (self.unknown if unknown else self.recent).add(fingerprint)
        self.reasons.update(blockers)
        entry=dict(fingerprint=fingerprint,object=object_id,requested_profile=self.profile,
            observed_at=self.now.isoformat(),publication_at=record['publication_at'],provenance=provenance,
            ai_eligible=False,verification_blockers=sorted(blockers))
        path=self.runs/(self.run_id+('.quarantine.jsonl' if unknown or record['conflict'] else '.recent.jsonl'))
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as stream:stream.write(json.dumps(entry,sort_keys=True)+'\n');stream.flush();os.fsync(stream.fileno())

    def summary(self,status,reason):
        classes=Counter(self.state['records'][key]['media_class'] for key in self.recent)
        return dict(run_id=self.run_id,mode=self.mode,status=status,reason=reason,profile=self.profile,
            run_date=self.local.date().isoformat(),timezone='America/Los_Angeles',cutoff=self.cutoff.isoformat(),
            started_at=self.now.isoformat(),
            recent_candidates_collected=len(self.recent),unknown_timestamp_candidates=len(self.unknown),
            media_counts=dict(classes),counts=dict(self.counts),
            review_counts=dict(Counter(self.state['records'][key].get('review_status','review_required') for key in self.recent)),verification_blockers=dict(self.reasons),
            identity_verified_candidates=sum(self.state['records'][key]['identity_verified'] for key in self.recent),
            identity_conflicts=sum(record['conflict'] for record in self.state['records'].values()),
            newly_discovered_verified_posts=0,ai_eligible=0,source_membership_verified=False,
            database_writes=0,gemini_requests=0,openai_requests=0,production_handoff='disabled')

    def checkpoint(self,status,reason):
        atomic(self.root/'state.json',self.state)
        active={key:record for key,record in self.state['records'].items() if not record['unknown_timestamp'] and
            not record['conflict'] and record['publication_at'] and datetime.fromisoformat(record['publication_at'])>=self.cutoff}
        atomic(self.root/'active_recent.json',dict(cutoff=self.cutoff.isoformat(),candidates=active,ai_eligible=0))
        atomic(self.runs/(self.run_id+'.summary.json'),self.summary(status,reason))

    def finish(self,capture):
        self.capture_result=capture
        health=capture.get('scrape_health',{})
        # Staging incomplete responses is intentional, but never masks login,
        # transport, browser, or execution failures as successful collection.
        blocking=capture.get('status')=='interrupted' or health.get('reason') not in (
            'timeline_verified','response_capture_failed','no_valid_timeline_response')
        blocking=blocking or any(health.get('error_categories',{}).get(key,0) for key in (
            'response_body_unavailable','json_decoding_failure','browser_context_closed',
            'response_processing_exception','http_error','request_failed'))
        observed=bool(self.recent or self.unknown or self.counts['older_observations_excluded'])
        completed=not blocking and (observed or health.get('valid_empty_timeline_responses',0)>0)
        status='completed_staging_only' if completed else 'failed'
        reason='bounded_capture_finished_coverage_unverified' if completed else health.get('reason','capture_failed')
        if completed:
            self.state['collection_days'][self.profile]=self.local.date().isoformat()
            if self.profile=='dvcatucsd':self.state['last_collection_day']=self.local.date().isoformat()
        self.checkpoint(status,reason)
        output=self.summary(status,reason)
        output['capture']=capture
        output['ended_at']=datetime.now(timezone.utc).isoformat()
        atomic(self.runs/(self.run_id+'.summary.json'),output)
        return output
