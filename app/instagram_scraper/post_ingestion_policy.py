"""Offline-only per-post policy. No ingestion, persistence, or network operations.

Evidence objects are trusted adapter attestations, not proof provided by Python
types. No production evidence producer or activation flag exists. Never construct
attestations from client JSON, author matches, coauthor hints or opaque doc_ids.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.instagram_collaboration import (
    CollaborationMetadata, account_name, normalize_collaboration, compare_post_identity,
)
from app.instagram_scraper.timeline_media import resolve_timeline_media
from app.parent_pipeline.screening import screen


@dataclass(frozen=True)
class ProfileLinkEvidence:
    """Independently reviewed evidence bound to this post, author and profile.

    reviewed_request_contract must establish this exact media's profile membership,
    not just the target of an unrelated request. independent_post_verification must
    establish the same identity/link through a separately trusted source.
    """
    post_shortcode: str
    primary_author: str
    requested_profile: str
    relation: str  # author | confirmed_collaborator
    source: str  # reviewed_request_contract | independent_post_verification


@dataclass(frozen=True)
class ErrorIsolationEvidence:
    """Reviewed attribution of ALL response errors away from this exact post.

    required_fields_unaffected includes identity, author/collaboration, timestamp,
    caption, all media/carousel fields and pagination needed for this post's media.
    Visible fields alone, an optional-looking path or another post index do not
    establish this. No current unknown GraphQL path can produce this evidence.
    """
    post_shortcode: str
    primary_author: str
    all_errors_attributed: bool
    required_fields_unaffected: bool


@dataclass(frozen=True)
class PostObservation:
    edge: object
    requested_profile: str
    authenticated: bool = False
    response_has_errors: bool = True  # Fail closed when the caller has not inspected errors.
    profile_evidence: ProfileLinkEvidence | None = None
    collaboration_evidence: tuple = ()
    error_isolation: ErrorIsolationEvidence | None = None


def _usable_image(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and bool(parsed.path) and not parsed.fragment and
                parsed.username is None and parsed.password is None and parsed.port in (None, 443) and
                any(host == base or host.endswith('.' + base)
                    for base in ('cdninstagram.com', 'fbcdn.net', 'instagram.com')))
    except ValueError:
        return False


def _has_image(media):
    # Fixed poster-image positions only; never avatar/video URLs or recursive search.
    if any(_usable_image(media.get(key)) for key in ('display_url', 'thumbnail_src')):
        return True
    for key in ('image_versions2', 'image_versions'):
        value = media.get(key)
        candidates = value.get('candidates') if isinstance(value, dict) else None
        if isinstance(candidates, list) and any(isinstance(item, dict) and _usable_image(item.get('url'))
                                              for item in candidates[:128]):
            return True
    resources = media.get('display_resources')
    return isinstance(resources, list) and any(isinstance(item, dict) and _usable_image(item.get('src'))
                                              for item in resources[:128])


def _media_reasons(media):
    reasons = set()
    if 'media_type' in media and (type(media['media_type']) is not int or media['media_type'] not in (1, 2, 8)):
        reasons.add('invalid_media_type')
    expected_type = {'GraphImage': 1, 'GraphVideo': 2, 'GraphSidecar': 8}.get(media.get('__typename'))
    if expected_type is not None and 'media_type' in media and media['media_type'] != expected_type:
        reasons.add('conflicting_media_type')
    carousel = (media.get('media_type') == 8 or media.get('__typename') == 'GraphSidecar' or
                'carousel_media' in media or 'edge_sidecar_to_children' in media)
    if not carousel:
        if not (type(media.get('media_type')) is int and media['media_type'] in (1, 2) or
                media.get('__typename') in ('GraphImage', 'GraphVideo')):
            reasons.add('media_type_unverified')
        if not _has_image(media):
            reasons.add('missing_usable_image')
        return reasons
    if 'carousel_media' in media:
        children = media['carousel_media']; count = media.get('carousel_media_count')
        if 'edge_sidecar_to_children' in media:
            reasons.add('conflicting_carousel_representations')
    else:
        connection = media.get('edge_sidecar_to_children')
        if not isinstance(connection, dict) or not isinstance(connection.get('edges'), list):
            return {'incomplete_carousel'}
        edges = connection['edges']; count = connection.get('count')
        children = [edge.get('node') if isinstance(edge, dict) else None for edge in edges[:101]]
        page = connection.get('page_info')
        if not isinstance(page, dict) or page.get('has_next_page') is not False:
            reasons.add('carousel_pagination_unverified')
    if not isinstance(children, list) or not 1 <= len(children) <= 100:
        return reasons | {'incomplete_carousel'}
    if type(count) is not int or count != len(children):
        reasons.add('carousel_count_unverified')
    seen = set()
    for child in children:
        if not isinstance(child, dict):
            reasons.add('incomplete_carousel'); continue
        identity = child.get('pk', child.get('id'))
        if not (type(identity) is int and identity > 0 or isinstance(identity, str) and
                identity.isascii() and identity.isdecimal() and 0 < len(identity) <= 32 and int(identity) > 0):
            reasons.add('invalid_carousel_child_identity')
        elif str(identity) in seen:
            reasons.add('duplicate_carousel_child')
        else:
            seen.add(str(identity))
        if not _has_image(child):
            reasons.add('missing_carousel_child_image')
    return reasons


def _evaluate(observation, now, max_age_days, *, inspect_media=True):
    reasons = set(); screening = None
    media, resolution = resolve_timeline_media(observation.edge)
    if media is None:
        return None, {}, dict(status='skipped', reasons=['media_' + resolution], screening=None)
    metadata = normalize_collaboration(media, observation.requested_profile,
                                       accepted_evidence=observation.collaboration_evidence)
    if metadata.post_shortcode is None:
        reasons.add('invalid_post_identity')
    if metadata.primary_author is None:
        reasons.add('invalid_primary_author')
    if metadata.requested_profile is None:
        reasons.add('invalid_requested_profile')
    # Other unverified collaborators do not invalidate independently proven primary
    # authorship. They also never establish requested-profile membership.
    for reason in metadata.review_reasons:
        if reason not in ('collaboration_unverified_or_pending', 'discovery_profile_differs_from_author'):
            reasons.add(reason)
    if observation.authenticated is not True:
        reasons.add('authentication_unverified')
    proof = observation.profile_evidence
    linked = (isinstance(proof, ProfileLinkEvidence) and
              proof.source in ('reviewed_request_contract', 'independent_post_verification') and
              proof.post_shortcode == metadata.post_shortcode and metadata.post_shortcode is not None and
              account_name(proof.primary_author) == metadata.primary_author and metadata.primary_author is not None and
              account_name(proof.requested_profile) == metadata.requested_profile and metadata.requested_profile is not None)
    if not linked:
        reasons.add('profile_link_unverified')
    elif proof.relation == 'author':
        if metadata.primary_author != metadata.requested_profile:
            reasons.add('profile_author_conflict')
    elif proof.relation == 'confirmed_collaborator':
        if not any(item.username == metadata.requested_profile and item.status == 'confirmed'
                   for item in metadata.collaborators):
            reasons.add('collaboration_not_confirmed')
    else:
        reasons.add('profile_relation_unverified')
    if observation.response_has_errors is not False:
        isolated = observation.error_isolation
        if not (isinstance(isolated, ErrorIsolationEvidence) and
                isolated.post_shortcode == metadata.post_shortcode and metadata.post_shortcode is not None and
                account_name(isolated.primary_author) == metadata.primary_author and metadata.primary_author is not None and
                isolated.all_errors_attributed is True and isolated.required_fields_unaffected is True):
            reasons.add('graphql_errors_unresolved_for_post')
    stamp = media.get('taken_at', media.get('device_timestamp'))
    published = None
    try:
        if type(stamp) is not int or stamp <= 0:
            raise ValueError()
        published = datetime.fromtimestamp(stamp, timezone.utc)
        if published > now + timedelta(minutes=5):
            reasons.add('publication_in_future')
        if published < now - timedelta(days=max_age_days):
            reasons.add('post_outside_freshness_window')
        if 'taken_at' in media and 'device_timestamp' in media and media['taken_at'] != media['device_timestamp']:
            reasons.add('publication_timestamp_conflict')
    except (ValueError, OverflowError, OSError):
        reasons.add('invalid_publication_timestamp')
    caption = media.get('caption')
    if not isinstance(caption, dict) or not isinstance(caption.get('text'), str):
        reasons.add('missing_or_invalid_caption')
    elif len(caption['text']) > 20000:
        reasons.add('caption_inspection_limit')
    if inspect_media:
        reasons.update(_media_reasons(media))
    if (published and isinstance(caption, dict) and isinstance(caption.get('text'), str)
            and len(caption['text']) <= 20000):
        screening = screen(dict(caption=caption['text'], posted_time=published), now)
        if screening['status'] != 'eligible':
            reasons.add('event_expired' if screening['status'] == 'skipped_expired' else 'event_screening_requires_review')
    # Internal conflict signature deliberately excludes signed CDN URL variation.
    child_list = media.get('carousel_media')
    connection = media.get('edge_sidecar_to_children')
    if child_list is None and isinstance(connection, dict) and isinstance(connection.get('edges'), list):
        child_list = [edge.get('node') if isinstance(edge, dict) else None for edge in connection['edges'][:101]]
    child_ids = tuple(str(child.get('pk', child.get('id'))) if isinstance(child, dict) else None
                      for child in child_list[:101]) if isinstance(child_list, list) else ()
    signature = (stamp, caption.get('text') if isinstance(caption, dict) else None,
                 media.get('media_type'), media.get('__typename'), child_ids)
    review = any(reason in reasons for reason in ('profile_link_unverified', 'collaboration_not_confirmed',
        'graphql_errors_unresolved_for_post', 'event_screening_requires_review', 'authentication_unverified'))
    return metadata, signature, dict(status='review_required' if review else 'skipped' if reasons else 'eligible_offline',
                                    reasons=sorted(reasons), screening=screening)



def inspect_non_image_requirements(observation, *, now, max_age_days=40):
    """Diagnostic-only checks for a pilot with a separate image validator.

    Never returns eligibility. Normal evaluate_posts still requires its original
    media checks by default; this does not bypass any production policy gate.
    """
    if (not isinstance(observation,PostObservation) or not isinstance(now,datetime) or now.utcoffset() is None
            or type(max_age_days) is not int or max_age_days<=0):
        raise ValueError('Valid observation and aware policy clock required')
    _,_,decision=_evaluate(observation,now,max_age_days,inspect_media=False)
    return dict(decision,status='inspection_only',
                reasons=sorted(set(decision['reasons'])|{'production_handoff_disabled'}))


def evaluate_posts(observations, *, now, max_age_days=40, existing_posts=()):
    """Evaluate a bounded batch without returning raw posts or enabling writes.

    Existing identity snapshots are read-only inputs. Duplicate grouping never
    selects a replacement PostgreSQL ID or changes a parent marker. All colliding
    observations are quarantined on an identity/meaningful-data conflict.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('Timezone-aware policy clock required')
    if type(max_age_days) is not int or max_age_days <= 0:
        raise ValueError('Positive freshness window required')
    if not isinstance(observations, (tuple, list)) or len(observations) > 1000:
        raise ValueError('Bounded observation batch required')
    if not isinstance(existing_posts, (tuple, list)) or len(existing_posts) > 10000 or any(
            not isinstance(item, CollaborationMetadata) for item in existing_posts):
        raise ValueError('Bounded existing identity snapshots required')
    rows = []
    for observation in observations:
        if not isinstance(observation, PostObservation):
            rows.append((None, None, dict(status='skipped', reasons=['malformed_observation'], screening=None)))
            continue
        try:
            rows.append(_evaluate(observation, now, max_age_days))
        except Exception:
            rows.append((None, None, dict(status='review_required', reasons=['validation_exception'], screening=None)))
    def reject(index, reason, status='review_required'):
        decision = rows[index][2]
        decision.update(status=status, reasons=sorted(set(decision['reasons']) | {reason}))
    conflicted_codes = set()
    for index, (metadata, signature, _) in enumerate(rows):
        if metadata is None or metadata.post_shortcode is None:
            continue
        for previous in range(index):
            other, other_signature, _ = rows[previous]
            if other is None:
                continue
            relation = compare_post_identity(metadata, other)
            if relation == 'identity_conflict' or (relation == 'same_post_candidate' and signature != other_signature):
                reject(index, 'post_identity_or_data_conflict'); reject(previous, 'post_identity_or_data_conflict')
                conflicted_codes.update((metadata.post_shortcode, other.post_shortcode))
        for existing in existing_posts:
            relation = compare_post_identity(metadata, existing)
            if relation == 'identity_conflict':
                reject(index, 'existing_identity_conflict')
                conflicted_codes.add(metadata.post_shortcode)
            elif metadata.post_shortcode == existing.post_shortcode:
                reject(index, 'existing_post_identity' if relation == 'same_post_candidate' else
                       'existing_identity_requires_review', 'skipped' if relation == 'same_post_candidate' else 'review_required')
    for index, (metadata, _, _) in enumerate(rows):
        if metadata is not None and metadata.post_shortcode in conflicted_codes:
            reject(index, 'post_identity_or_data_conflict')
    # Do not combine fields from duplicates. Only a complete independently verified
    # observation can win; a prior incomplete observation cannot crowd it out.
    seen = set()
    for index, (metadata, _, decision) in enumerate(rows):
        if decision['status'] != 'eligible_offline':
            continue
        if metadata.post_shortcode in seen:
            reject(index, 'duplicate_post_identity', 'skipped')
        else:
            seen.add(metadata.post_shortcode)
    decisions = [dict(index=index, **row[2]) for index, row in enumerate(rows)]
    return dict(mode='offline_policy', production_ingestion_enabled=False,
                timeline_completeness='not_required', database_writes=0,
                counts={status:sum(item['status'] == status for item in decisions)
                        for status in ('eligible_offline', 'skipped', 'review_required')}, posts=decisions)
