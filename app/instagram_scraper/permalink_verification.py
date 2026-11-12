"""Offline-only image permalink verification, not connected to ingestion.

All evidence is private trusted-adapter state, never client JSON. A reviewed DOM
and payload contract is mandatory; this module claims no live Instagram schema
has been reviewed. No browser launch, navigation, HTTP, DB or persistence occurs.
"""
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit

from app.instagram_collaboration import account_name, normalize_collaboration
from app.instagram_scraper.post_ingestion_policy import (
    PostObservation, ProfileLinkEvidence, evaluate_posts, _usable_image,
)
from app.instagram_scraper.profile_grid_evidence import GridMembership, NavigationContext
from app.instagram_scraper.media_identity import numeric_id, normalize_identity

TARGET = 'dvcatucsd'
MAX_POSTS = 25
MAX_CHILDREN = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ROOTS = frozenset(('shortcode_media', 'xdt_shortcode_media'))


@dataclass(frozen=True)
class ReviewedPermalinkContract:
    """Developer review, not inferred from URL/author or supplied by user input.

    The review must establish a single primary-post article, primary-author link,
    complete caption, publication time, canonical identity and exact media root.
    Selectors for coauthors, suggested posts or truncated captions do not qualify.
    No production contract/default selectors are provided by this module.
    """
    review_reference: str
    media_root: str
    article_selector: str
    primary_author_selector: str
    caption_selector: str
    time_selector: str
    complete_caption_reviewed: bool = False


@dataclass(frozen=True)
class Candidate:
    permalink: str = field(repr=False)
    shortcode: str = field(repr=False)
    primary_author: str = field(repr=False)
    media_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PermalinkPageEvidence:
    """Independently observed reviewed DOM, not copied from captured GraphQL."""
    shortcode: str = field(repr=False)
    primary_author: str = field(repr=False)
    published_at: datetime = field(repr=False)
    caption: str = field(repr=False)
    observed_at: datetime
    document_epoch: float
    authenticated: bool
    review_reference: str


@dataclass(frozen=True)
class Observation:
    candidate: Candidate = field(repr=False)
    payload: object = field(repr=False)
    page_evidence: PermalinkPageEvidence | None = field(default=None, repr=False)
    authenticated: bool = False
    collaboration_evidence: tuple = field(default=(), repr=False)


@dataclass(frozen=True)
class VerifiedImagePost:
    """Private handoff only; raw media/URLs/captions must not be logged/serialized."""
    candidate: Candidate = field(repr=False)
    media: dict = field(repr=False)
    image_references: tuple[str, ...] = field(repr=False)
    # This is offline eligibility, not permission to write or proof of a complete feed.


class Unverified(ValueError):
    """Only fixed authored reason codes; never upstream values or exceptions."""


def _code(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}',value,re.ASCII) else None


def _id(value):
    return numeric_id(value)


def _url_path(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 for c in value):
        return None
    try:
        u=urlsplit(value)
        if (u.scheme!='https' or u.hostname not in ('instagram.com','www.instagram.com') or
                u.username or u.password or u.port not in (None,443) or u.query or u.fragment or '%' in u.path):
            return None
        return u.path
    except ValueError:return None


def _permalink(value):
    path=_url_path(value)
    match=re.fullmatch(r'/p/([A-Za-z0-9_-]{1,32})/',path,re.ASCII) if path else None
    return match[1] if match else None


def _author_link(value):
    path=_url_path(value)
    match=re.fullmatch(r'/([A-Za-z0-9._]{1,30})/?',path,re.ASCII) if path else None
    return account_name(match[1]) if match else None


def _contract_valid(contract):
    return (isinstance(contract,ReviewedPermalinkContract) and isinstance(contract.media_root,str) and contract.media_root in ROOTS and
            contract.complete_caption_reviewed is True and
            all(isinstance(v,str) and 0<len(v)<=512 for v in
                (contract.review_reference,contract.article_selector,contract.primary_author_selector,
                 contract.caption_selector,contract.time_selector)))


def _clock(value):
    return isinstance(value,datetime) and value.utcoffset() is not None


_READ_POST = r'''(c) => {
  const one = (root, selector) => {
    const nodes = root.querySelectorAll(selector);
    return nodes.length === 1 ? nodes[0] : null;
  };
  const article = one(document, c.article_selector);
  const author = article && one(article, c.primary_author_selector);
  const caption = article && one(article, c.caption_selector);
  const time = article && one(article, c.time_selector);
  const canonical = one(document, 'link[rel="canonical"]');
  const visible = e => !!e && e.getClientRects().length > 0 &&
    getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
  const scoped = !!(article && article.tagName === 'ARTICLE' && visible(article) &&
    visible(author) && visible(caption) && visible(time) && author.tagName === 'A' &&
    time.tagName === 'TIME' && !article.closest('aside, nav, [role="dialog"]'));
  const text = caption ? caption.textContent : null;
  return {url: location.href, epoch: performance.timeOrigin, scoped,
    canonical_url: canonical ? canonical.href : null,
    author_url: author ? author.href : null,
    published_at: time ? time.getAttribute('datetime') : null,
    caption: typeof text === 'string' && text.length <= 20000 ? text : null};
}'''


def capture_loaded_permalink(page, candidate, *, now, contract=None, authenticated=False):
    """Read only an already loaded page; never navigate, click, scroll or launch.

    Requires a reviewed contract and a separately verified authenticated context.
    Caller must establish candidate/profile membership elsewhere. Returns private
    page evidence or None; failures never reveal DOM/URLs/exception messages.
    """
    if (not _contract_valid(contract) or not isinstance(candidate,Candidate) or not _clock(now) or
            authenticated is not True or not _code(candidate.shortcode) or
            _permalink(candidate.permalink)!=candidate.shortcode):
        return None
    try:
        before=page.evaluate('() => ({url: location.href, epoch: performance.timeOrigin})')
        raw=page.evaluate(_READ_POST,vars(contract))
        after=page.evaluate('() => ({url: location.href, epoch: performance.timeOrigin})')
        if not all(isinstance(value,dict) for value in (before,raw,after)):return None
        epoch=raw.get('epoch')
        if (type(epoch) not in (int,float) or not 0<epoch<10**16 or raw.get('scoped') is not True or
                any(value.get('epoch')!=epoch or _permalink(value.get('url'))!=candidate.shortcode
                    for value in (before,raw,after)) or _permalink(raw.get('canonical_url'))!=candidate.shortcode):
            return None
        author=_author_link(raw.get('author_url'))
        stamp=datetime.fromisoformat(raw['published_at'].replace('Z','+00:00'))
        caption=raw.get('caption')
        if (author!=account_name(candidate.primary_author) or not author or not _clock(stamp) or
                not isinstance(caption,str) or not caption.strip() or len(caption)>20000):return None
        return PermalinkPageEvidence(candidate.shortcode,author,stamp.astimezone(timezone.utc),caption,
                                     now,epoch,True,contract.review_reference)
    except Exception:return None


def _bounded(payload):
    stack=[(payload,0)];count=0;size=0;seen=set()
    while stack:
        value,depth=stack.pop();count+=1
        if depth>16 or count>10000:raise Unverified('payload_inspection_limit')
        if isinstance(value,(dict,list)):
            if id(value) in seen:raise Unverified('ambiguous_payload_graph')
            seen.add(id(value))
        if isinstance(value,dict):
            if len(value)>128 or any(not isinstance(k,str) or len(k)>128 for k in value):
                raise Unverified('payload_inspection_limit')
            size+=sum(len(k.encode()) for k in value)
            stack.extend((v,depth+1) for v in value.values())
        elif isinstance(value,list):
            if len(value)>256:raise Unverified('payload_inspection_limit')
            stack.extend((v,depth+1) for v in value)
        elif isinstance(value,str):
            if len(value)>20000:raise Unverified('payload_inspection_limit')
            size+=len(value.encode())
        elif value is not None and type(value) not in (int,float,bool):raise Unverified('malformed_payload')
        if size>MAX_RESPONSE_BYTES:raise Unverified('payload_inspection_limit')


def _image_reference(media):
    candidates=[media.get('display_url'),media.get('thumbnail_src')]
    for name in ('image_versions2','image_versions'):
        if name in media:
            value=media[name]
            refs=value.get('candidates') if isinstance(value,dict) else None
            if not isinstance(refs,list) or len(refs)>128:raise Unverified('incomplete_image_references')
            candidates.extend(item.get('url') if isinstance(item,dict) else None for item in refs)
    if 'display_resources' in media:
        refs=media['display_resources']
        if not isinstance(refs,list) or len(refs)>128:raise Unverified('incomplete_image_references')
        candidates.extend(item.get('src') if isinstance(item,dict) else None for item in refs)
    return next((value for value in candidates if _usable_image(value)),None)


def _kind(media):
    numeric=media.get('media_type');typename=media.get('__typename')
    if ('media_type' in media and (type(numeric) is not int or numeric not in (1,2,8)) or
            '__typename' in media and typename not in ('GraphImage','GraphVideo','GraphSidecar')):
        raise Unverified('unknown_media_type')
    named={'GraphImage':1,'GraphVideo':2,'GraphSidecar':8}.get(typename)
    if named is not None and numeric is not None and named!=numeric:raise Unverified('conflicting_media_type')
    kind=numeric if numeric is not None else named
    if kind not in (1,8):raise Unverified('video_or_unknown_media')
    if ('is_video' in media and media['is_video'] is not False or
            any(key in media for key in ('video_url','video_versions','video_dash_manifest','clips_metadata')) or
            media.get('product_type') not in (None,'feed','carousel_container')):
        raise Unverified('video_or_unknown_media')
    return kind


def _media_identity(media):
    try:return normalize_identity(media)
    except ValueError as exc:raise Unverified(str(exc)) from None


def _normalize_post_fields(media):
    """Existing identity/caption/timestamp checks, independent of image layout."""
    if not isinstance(media,dict):raise Unverified('incomplete_post')
    result=dict(media)
    codes=[_code(media[key]) for key in ('code','shortcode') if key in media]
    if not codes or None in codes or len(set(codes))!=1:raise Unverified('conflicting_post_identity')
    result['code']=codes[0];result['pk']=_media_identity(media)
    stamps=[media[key] for key in ('taken_at','taken_at_timestamp','device_timestamp') if key in media]
    if not stamps or any(type(v) is not int or v<=0 for v in stamps) or len(set(stamps))!=1:
        raise Unverified('conflicting_or_missing_publication_timestamp')
    result['taken_at']=stamps[0]
    captions=[]
    if 'caption' in media:
        value=media['caption']
        if not isinstance(value,dict) or not isinstance(value.get('text'),str):raise Unverified('incomplete_caption')
        captions.append(value['text'])
    if 'edge_media_to_caption' in media:
        connection=media['edge_media_to_caption'];edges=connection.get('edges') if isinstance(connection,dict) else None
        if not isinstance(edges,list) or len(edges)!=1:raise Unverified('incomplete_caption')
        value=edges[0].get('node') if isinstance(edges[0],dict) else None
        if not isinstance(value,dict) or not isinstance(value.get('text'),str):raise Unverified('incomplete_caption')
        captions.append(value['text'])
    if not captions or not captions[0].strip() or len(set(captions))!=1 or len(captions[0])>20000:
        raise Unverified('incomplete_or_conflicting_caption')
    result['caption']={'text':captions[0]}
    return result


def _normalize(media):
    result=_normalize_post_fields(media)
    kind=_kind(media)
    has_children='carousel_media' in media or 'edge_sidecar_to_children' in media
    if kind==1:
        if has_children:raise Unverified('conflicting_media_type')
        image=_image_reference(media)
        if image is None:raise Unverified('missing_usable_image')
        return result,(image,)
    if 'carousel_media' in media and 'edge_sidecar_to_children' in media:
        raise Unverified('conflicting_carousel_representations')
    if 'carousel_media' in media:
        children=media['carousel_media'];count=media.get('carousel_media_count')
    else:
        connection=media.get('edge_sidecar_to_children')
        if not isinstance(connection,dict):raise Unverified('incomplete_carousel')
        edges=connection.get('edges');count=connection.get('count');page=connection.get('page_info')
        if (not isinstance(edges,list) or not isinstance(page,dict) or page.get('has_next_page') is not False):
            raise Unverified('incomplete_carousel')
        children=[edge.get('node') if isinstance(edge,dict) else None for edge in edges]
    if not isinstance(children,list) or not 1<=len(children)<=MAX_CHILDREN or type(count) is not int or count!=len(children):
        raise Unverified('incomplete_carousel')
    seen={_media_identity(media)};images=[]
    for child in children:
        if not isinstance(child,dict):raise Unverified('incomplete_carousel')
        identity=_media_identity(child)
        if identity in seen:raise Unverified('duplicate_carousel_child')
        seen.add(identity)
        if _kind(child)!=1 or 'carousel_media' in child or 'edge_sidecar_to_children' in child:
            raise Unverified('non_image_carousel_child')
        image=_image_reference(child)
        if image is None:raise Unverified('missing_carousel_image')
        images.append(image)
    return result,tuple(images)


def _membership_verified(membership,navigation,code,now):
    return (isinstance(membership,GridMembership) and isinstance(navigation,NavigationContext) and
            membership.context==navigation and navigation.profile==TARGET and membership.authenticated is True and
            type(navigation.document_epoch) in (float,int) and 0<navigation.document_epoch<10**16 and
            isinstance(membership.shortcodes,frozenset) and len(membership.shortcodes)<=1000 and code in membership.shortcodes and
            _clock(membership.observed_at) and timedelta(0)<=now-membership.observed_at<=timedelta(minutes=5))


def verify_batch(observations, *, now, contract=None, membership=None, profile_navigation=None,
                 existing_posts=(), max_age_days=40):
    """Verify up to 25 candidates; reject per-post failures without aborting peers.

    Content evidence, profile membership and collaboration acceptance are separate.
    A post authored by the target is NOT by itself proof of Posts-grid membership.
    Returned verified_posts are private in-memory handoffs, not production ingestion.
    """
    if not _clock(now) or type(max_age_days) is not int or max_age_days<=0:
        raise ValueError('Aware clock and positive freshness window required.')
    if not isinstance(observations,(list,tuple)) or len(observations)>MAX_POSTS:
        raise ValueError('At most 25 explicit post candidates required.')
    decisions=[];pending=[];private=[];tainted_codes=set()
    for index,observation in enumerate(observations):
        decision=dict(index=index,status='skipped',reasons=[],permalink_verified=False,
                      profile_membership_verified=False,image_count=0)
        decisions.append(decision)
        try:
            if not _contract_valid(contract):raise Unverified('reviewed_permalink_contract_missing')
            if not isinstance(observation,Observation) or not isinstance(observation.candidate,Candidate):
                raise Unverified('malformed_candidate')
            candidate=observation.candidate
            if (_permalink(candidate.permalink)!=_code(candidate.shortcode) or not _code(candidate.shortcode) or
                    account_name(candidate.primary_author) is None):raise Unverified('invalid_candidate_identity')
            if observation.authenticated is not True:raise Unverified('authentication_unverified')
            payload=observation.payload;_bounded(payload)
            if not isinstance(payload,dict) or not isinstance(payload.get('data'),dict):raise Unverified('incomplete_post')
            if 'errors' in payload and payload['errors']!=[]:raise Unverified('graphql_errors_unresolved_for_post')
            data=payload['data']
            if sum(name in data for name in ROOTS)!=1 or contract.media_root not in data:
                raise Unverified('ambiguous_or_unreviewed_media_root')
            media,images=_normalize(data[contract.media_root])
            metadata=normalize_collaboration(media,TARGET,accepted_evidence=observation.collaboration_evidence)
            if (metadata.post_shortcode!=candidate.shortcode or metadata.primary_author!=account_name(candidate.primary_author) or
                    candidate.media_id is not None and _id(candidate.media_id)!=metadata.instagram_media_id):
                raise Unverified('candidate_identity_or_author_mismatch')
            if 'author_owner_conflict' in metadata.review_reasons or 'invalid_owner' in metadata.review_reasons:
                raise Unverified('ambiguous_author_metadata')
            page=observation.page_evidence
            if (not isinstance(page,PermalinkPageEvidence) or page.authenticated is not True or
                    page.review_reference!=contract.review_reference or page.shortcode!=metadata.post_shortcode or
                    account_name(page.primary_author)!=metadata.primary_author or not _clock(page.observed_at) or
                    not timedelta(0)<=now-page.observed_at<=timedelta(minutes=5) or not _clock(page.published_at) or
                    page.published_at!=datetime.fromtimestamp(media['taken_at'],timezone.utc) or
                    not isinstance(page.caption,str) or ' '.join(page.caption.split())!=' '.join(media['caption']['text'].split()) or
                    type(page.document_epoch) not in (float,int) or not 0<page.document_epoch<10**16):
                raise Unverified('independent_permalink_evidence_unverified')
            decision.update(permalink_verified=True,image_count=len(images))
            linked=_membership_verified(membership,profile_navigation,candidate.shortcode,now)
            decision['profile_membership_verified']=bool(linked)
            proof=None
            if linked:
                relation='author' if metadata.primary_author==TARGET else 'confirmed_collaborator'
                proof=ProfileLinkEvidence(metadata.post_shortcode,metadata.primary_author,TARGET,relation,'independent_post_verification')
            pending.append(PostObservation({'node':media},TARGET,True,False,proof,observation.collaboration_evidence))
            private.append((index,VerifiedImagePost(candidate,copy.deepcopy(media),images)))
        except Unverified as exc:
            decision['reasons']=[str(exc)]
            if isinstance(observation,Observation) and isinstance(observation.candidate,Candidate):
                tainted_codes.add(_code(observation.candidate.shortcode))
        except Exception:
            decision['reasons']=['verification_failed']
            if isinstance(observation,Observation) and isinstance(observation.candidate,Candidate):
                tainted_codes.add(_code(observation.candidate.shortcode))
    policy=evaluate_posts(pending,now=now,max_age_days=max_age_days,existing_posts=existing_posts)
    verified=[]
    for (index,post),result in zip(private,policy['posts']):
        decision=decisions[index]
        decision.update(status=result['status'],reasons=result['reasons'])
        if not decision['profile_membership_verified']:
            decision['reasons']=sorted(set(decision['reasons'])|{'profile_membership_unverified'})
        if post.candidate.shortcode in tainted_codes:
            decision.update(status='review_required',reasons=sorted(set(decision['reasons'])|{'conflicting_or_incomplete_observations'}))
        if decision['status']=='eligible_offline':verified.append(post)
    return dict(summary=dict(mode='offline_permalink_verification',production_ingestion_enabled=False,
                            profile_timeline_complete=False,database_writes=0,posts=decisions,
                            eligible_count=len(verified),skipped_or_review_count=len(decisions)-len(verified)),
                verified_posts=tuple(verified))
