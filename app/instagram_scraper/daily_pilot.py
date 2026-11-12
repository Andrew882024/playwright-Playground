"""Disabled daily pilot and database-free per-post GraphQL qualification.

No source/accepted-collaboration evidence producer currently exists. This module
does not invent one: every current candidate is skipped. It never calls an upsert
or parent runner. A later reviewed integration is required for paid execution.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import stat

from app.instagram_collaboration import normalize_collaboration
from app.instagram_scraper.graphql_diagnostics import TIMELINE_ROOTS
from app.instagram_scraper.timeline_media import resolve_timeline_media
from app.instagram_scraper.permalink_verification import _normalize_post_fields, _media_identity, Unverified
from app.instagram_scraper.original_image_adapter import recover_original_images, ImageRecoveryError
from app.instagram_scraper.profile_grid_evidence import _isolated_errors
from app.instagram_scraper.post_ingestion_policy import PostObservation, inspect_non_image_requirements
from app.instagram_scraper.media_identity import inspect_identity, FAILURES

TARGET='dvcatucsd'
BLOCKERS=('source_verification_unavailable','accepted_collaboration_producer_unavailable',
          'live_verified_eligible_capture_missing','verified_ingestion_handoff_not_implemented',
          'cross_profile_canonical_import_handoff_missing','organizer_display_handoff_not_connected')
MEDIA_REASONS=frozenset('''unknown_media_type conflicting_media_type video_or_unknown_media
conflicting_or_missing_media_identity incomplete_post conflicting_post_identity
conflicting_or_missing_publication_timestamp incomplete_caption incomplete_or_conflicting_caption
incomplete_image_references missing_usable_image conflicting_carousel_representations
incomplete_carousel duplicate_carousel_child non_image_carousel_child missing_carousel_image'''.split())
MEDIA_REASONS=MEDIA_REASONS | FAILURES

IMAGE_RECOVERY_REASONS=frozenset("""malformed_media unknown_media_type unsupported_media_layout
conflicting_media_type video_excluded video_or_unknown_media unsupported_product_type
invalid_image_reference unsupported_image_reference invalid_image_dimensions image_reference_limit
incomplete_image_references missing_original_image_reference legacy_image_reference_mismatch
conflicting_image_assets conflicting_carousel_layout conflicting_carousel_count incomplete_carousel
non_image_carousel_child duplicate_carousel_asset""".split())


class GraphQLGate:
    """Aggregate only. Do not retain bodies/captions/media URLs or join peers' fields."""
    def __init__(self,now=None,max_age_days=40):
        self.now=now or datetime.now(timezone.utc);self.max_age_days=max_age_days
        if self.now.utcoffset() is None or type(max_age_days) is not int or max_age_days<=0:
            raise ValueError('Aware clock and positive age window required')
        self.counts=Counter();self.reasons=Counter();self.response_reasons=Counter();self._seen=set()
        self.identity_shapes=Counter();self.image_failures=Counter()

    def observe(self,payload,category):
        self.counts['responses']+=1
        root=payload.get('data') if isinstance(payload,dict) else None
        if not isinstance(root,dict):
            self.response_reasons['unsupported_response_structure']+=1;return
        remaining=128
        for name,connection in list(root.items())[:64]:
            if name not in TIMELINE_ROOTS:
                self.response_reasons['unsupported_connection']+=1;continue
            if not isinstance(connection,dict) or not isinstance(connection.get('edges'),list):
                self.response_reasons['malformed_connection']+=1;continue
            edges=connection['edges']
            if not edges:self.counts['empty_connections']+=1
            if len(edges)>remaining:self.response_reasons['response_inspection_limit']+=1
            for index,edge in enumerate(edges[:remaining]):
                remaining-=1;self.counts['observations']+=1
                media,kind=resolve_timeline_media(edge)
                if media is None:
                    self.counts['skipped']+=1;self.counts['image_not_inspected']+=1;self.reasons['media_'+kind]+=1;continue
                code=media.get('code')
                identity=inspect_identity(media)
                self.identity_shapes[(identity['pk_shape'],identity['id_shape'],identity['relation'])]+=1
                if isinstance(code,str) and code in self._seen:self.counts['duplicate_observations']+=1
                if isinstance(code,str) and len(self._seen)<5000:self._seen.add(code)
                metadata=normalize_collaboration(media,TARGET)
                errors=payload.get('errors')
                has_errors='errors' in payload and errors!=[]
                isolation=_isolated_errors(errors,name,index,edges,metadata) if has_errors else None
                reasons={'source_verification_unavailable'}
                if metadata.primary_author!=TARGET:
                    reasons.add('accepted_collaboration_unverified')
                images=None
                try:
                    # Image recovery neither normalizes numeric IDs nor attests
                    # source/authentication/GraphQL integrity. Preserve its child
                    # ARRAY order: image_urls[0] remains the primary visual reference.
                    images=recover_original_images(media)
                    self.counts['image_fields_valid']+=1
                    self.counts[images.layout]+=1
                    self.counts['original_images']+=len(images.image_urls)
                    self.counts['resolution_variants']+=images.variant_count
                except ImageRecoveryError as exc:
                    reason=str(exc)
                    reason=reason if reason in IMAGE_RECOVERY_REASONS else 'image_validation_failed'
                    self.image_failures[reason]+=1;reasons.add(reason)
                except Exception:
                    self.image_failures['image_validation_failed']+=1;reasons.add('image_validation_failed')
                try:
                    # Reuse unchanged identity/caption/time rules independently.
                    # A failure must not erase a successful image-only result.
                    _normalize_post_fields(media)
                    if images is not None and images.layout=='image_only_carousel':
                        children=media.get('carousel_media')
                        if children is None:
                            children=[edge['node'] for edge in media['edge_sidecar_to_children']['edges']]
                        seen={_media_identity(media)}
                        for child in children:
                            identity=_media_identity(child)
                            if identity in seen:raise Unverified('duplicate_carousel_child')
                            seen.add(identity)
                except Unverified as exc:
                    code=str(exc)
                    reasons.add(code if code in MEDIA_REASONS else 'invalid_image_post')
                except Exception:
                    reasons.add('post_validation_failed')
                try:
                    decision=inspect_non_image_requirements(PostObservation({'node':media},TARGET,
                        response_has_errors=has_errors,error_isolation=isolation),
                        now=self.now,max_age_days=self.max_age_days)
                    reasons.update(decision['reasons'])
                except Exception:reasons.add('post_validation_failed')
                if has_errors and isolation is None:reasons.add('graphql_errors_unresolved_for_post')
                # Unattended mode skips uncertain posts. It does not queue an
                # automatic paid retry or claim any source proof exists.
                self.counts['skipped']+=1
                self.reasons.update(reasons)
        if len(root)>64:self.response_reasons['response_inspection_limit']+=1

    def summary(self):
        return dict(accepted=0,skipped=self.counts['skipped'],observations=self.counts['observations'],
            duplicate_observations=self.counts['duplicate_observations'],
            image_fields_valid=self.counts['image_fields_valid'],responses=self.counts['responses'],
            image_only_validation=dict(passed=self.counts['image_fields_valid'],
                not_inspected=self.counts['image_not_inspected'],
                failed=sum(self.image_failures.values()),single_image=self.counts['single_image'],
                image_only_carousel=self.counts['image_only_carousel'],
                original_images=self.counts['original_images'],resolution_variants=self.counts['resolution_variants'],
                reason_counts=dict(sorted(self.image_failures.items()))),
            empty_connections=self.counts['empty_connections'],reason_counts=dict(sorted(self.reasons.items())),
            accepted_reason_counts={},skipped_reason_counts=dict(sorted(self.reasons.items())),
            diagnostic_reason_counts=dict(sorted(self.response_reasons.items())),
            identity_shapes=[dict(pk_shape=pk,id_shape=node,relation=relation,count=count)
                for (pk,node,relation),count in sorted(self.identity_shapes.items())],
            source_verification='unavailable',collaboration_verification='unavailable')


@contextmanager
def single_run_lock(directory=None):
    """Single-host nonblocking lock; separate from paid journals and legacy locks."""
    root=Path(directory or os.getenv('INSTAGRAM_DAILY_STATE_DIR') or
              Path.home()/'.local/state/event-master/instagram-daily-pilot')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=root.stat()
    if root.is_symlink() or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private pilot state directory required')
    fd=os.open(root/'run.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    held=False
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode & 0o077:
            raise ValueError('Private regular pilot lock required')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);held=True
        except BlockingIOError:pass
        yield held
    finally:
        if held:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def execute(*,validate_capture=False,run=False,profile=TARGET,gemini_cap=0,cover_cap=0,max_scrolls=1):
    if profile!=TARGET:raise ValueError('Only the pilot profile is supported')
    if validate_capture and run:raise ValueError('Choose capture validation or run')
    if any(type(value) is not int or value not in (0,1) for value in (gemini_cap,cover_cap)):
        raise ValueError('Pilot request limits must be zero or one')
    if type(max_scrolls) is not int or not 0<=max_scrolls<=2:
        raise ValueError('Pilot capture allows at most two scrolls')
    if validate_capture and (gemini_cap!=0 or cover_cap!=0):
        raise ValueError('Capture validation requires zero paid limits')
    enabled=(os.getenv('INSTAGRAM_DAILY_ENABLED') or '').strip().lower()=='true'
    result=dict(status='preflight',profile=TARGET,enabled=enabled,ready=False,
        activation_blockers=list(BLOCKERS),production_ingestion_enabled=False,
        gemini_max_attempts=gemini_cap,cover_max_attempts=cover_cap,
        gemini_http_requests=0,openai_generation_requests=0,database_writes=0,
        recovery='not_invoked',dashboard='not_invoked',event_master='not_invoked')
    if run:
        # An environment flag alone must NEVER route around missing source proof.
        return dict(result,status='blocked' if enabled else 'disabled',
                    reason='source_verification_unavailable' if enabled else 'daily_pilot_disabled')
    if not validate_capture:return result
    try:
        with single_run_lock() as held:
            if not held:return dict(result,status='busy',reason='daily_pilot_already_running')
            gate=GraphQLGate(max_age_days=int(os.getenv('INSTAGRAM_SCRAPE_MAX_AGE_DAYS') or '40'))
            from app.instagram_scraper.schema_capture import capture
            capture_result=capture(TARGET,headless=True,partial_capture=True,post_observer=gate.observe,max_scrolls=max_scrolls)
            health=capture_result.get('scrape_health',{})
            reason=health.get('reason')
            # Timeline-only health failures are reported intact but do not discard
            # independent per-post decisions. Auth/browser/transport failures do.
            fatal=reason not in ('timeline_verified','response_capture_failed',None)
            errors=health.get('error_categories',{})
            fatal |= any(errors.get(key,0) for key in ('request_failed','http_error','response_body_unavailable',
                'json_decoding_failure','browser_context_closed','response_processing_exception'))
            summary=gate.summary()
            fatal |= summary['observations']==0 and summary['empty_connections']==0
            return dict(result,status='failed' if fatal else 'completed_with_skips',
                reason='capture_unsuccessful' if fatal else 'no_verified_eligible_posts',
                per_post=summary,scrape_health=health,
                elapsed_seconds=capture_result.get('elapsed_seconds',0))
    except (Exception,SystemExit):
        return dict(result,status='failed',reason='capture_or_lock_unavailable')
