"""Visible-field diagnostics only. No posts are returned, persisted or approved."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import hmac
import os
import re
from urllib.parse import urlsplit

from app.instagram_scraper.graphql_diagnostics import TIMELINE_ROOTS, _add_pattern
from app.instagram_scraper.timeline_media import resolve_timeline_media
from app.instagram_scraper import request_profile_binding as binding

REASONS=('invalid_identity','source_account_missing','source_account_mismatch',
         'invalid_timestamp','missing_caption','invalid_caption','missing_image',
         'media_type_unverified','carousel_count_unverified','carousel_incomplete',
         'invalid_media_structure','validation_exception','identity_tracking_limit')
COUNTERS=('responses_inspected','candidate_media_nodes','unique_post_identities',
          'fully_valid_candidates','unique_fully_valid_candidates','incomplete_candidates',
          'duplicate_candidates','null_media_slots','malformed_media_slots',
          'unsupported_media_slots','responses_with_graphql_errors',
          'responses_rejected_by_production','inspection_truncated_responses','diagnostic_errors')
MAX_IDENTITIES=5000
CODE=re.compile(r'[A-Za-z0-9_-]{1,32}\Z',re.ASCII)
ACCOUNT=re.compile(r'[A-Za-z0-9._]{1,30}\Z',re.ASCII)


PROVENANCE_FLAGS=('response_has_errors','requested_account_in_coauthor_hint','coauthor_inspection_truncated',
                  'numeric_target_hint')
PROVENANCE_ENUMS=dict(
    request_binding=binding.BINDINGS,
    request_username_hint_relation=binding.RELATIONS,
    connection_kind=('known_timeline_root','timeline_name_hint','generic_media_edges','unknown'),
    wrapper_kind=('flat_node','node_media','unknown'),
    author_relation=('matches_requested','different_account','missing_or_invalid'),
    owner_relation=('matches_requested','different_account','missing_or_invalid'),
    owner_author_relation=('agree','disagree','unavailable'),
    coauthor_shape=('absent','null','empty_list','user_object_list','malformed','truncated'),
    validation_outcome=('visible_fields_valid','incomplete','unknown'))


def _username(value):
    name=value.get('username') if isinstance(value,dict) else None
    return name.casefold() if isinstance(name,str) and ACCOUNT.fullmatch(name) else None


def _provenance(media,account,connection_kind,wrapper_kind,has_errors,outcome):
    def relation(name):
        return 'missing_or_invalid' if name is None else 'matches_requested' if name==account else 'different_account'
    author=_username(media.get('user'));owner=_username(media.get('owner'))
    shape='absent';target=False;truncated=False
    # Fixed-position hint only. This field's presence does NOT prove accepted
    # collaboration, profile membership, or that its semantics are authoritative.
    if 'coauthor_producers' in media:
        values=media['coauthor_producers']
        if values is None:shape='null'
        elif not isinstance(values,list):shape='malformed'
        else:
            truncated=len(values)>32
            names=[_username(value) for value in values[:32]]
            target=account in names
            shape=('truncated' if truncated else 'empty_list' if not values else
                   'malformed' if any(name is None for name in names) else 'user_object_list')
    return dict(connection_kind=connection_kind,wrapper_kind=wrapper_kind,
                response_has_errors=has_errors,author_relation=relation(author),owner_relation=relation(owner),
                owner_author_relation='unavailable' if author is None or owner is None else
                    'agree' if author==owner else 'disagree',coauthor_shape=shape,
                requested_account_in_coauthor_hint=target,coauthor_inspection_truncated=truncated,
                validation_outcome=outcome,count=1)


def _usable_url(value):
    if not isinstance(value,str) or len(value)>8192:return False
    try:
        url=urlsplit(value)
        host=(url.hostname or '').lower()
        allowed=any(host==base or host.endswith('.'+base) for base in
                    ('cdninstagram.com','fbcdn.net','instagram.com'))
        return (url.scheme=='https' and allowed and url.username is None and url.password is None
                and url.port in (None,443) and bool(url.path) and not url.fragment
                and not any(ord(char)<33 for char in value))
    except ValueError:return False


def _images(media):
    # Reuse production URL extraction on an image-only projection. Never count
    # video_url/video_versions or account avatar URLs as poster images.
    from app.instagram_scraper.sync_ins import _post_media_cdn_urls_from_timeline_node
    projection={key:media[key] for key in ('display_url','thumbnail_src','image_versions2',
                                          'image_versions','display_resources') if key in media}
    return any(_usable_url(url) for url in _post_media_cdn_urls_from_timeline_node(projection))


def _carousel(media):
    """Require explicit counts and all distinct image-bearing child objects."""
    if isinstance(media.get('carousel_media'),list):
        children=media['carousel_media'];count=media.get('carousel_media_count');next_page=False
    else:
        connection=media.get('edge_sidecar_to_children')
        if not isinstance(connection,dict) or not isinstance(connection.get('edges'),list):
            return {'carousel_incomplete'},False
        edges=connection['edges']
        children=[edge.get('node') if isinstance(edge,dict) else None for edge in edges]
        count=connection.get('count')
        page_info=connection.get('page_info')
        next_page=isinstance(page_info,dict) and page_info.get('has_next_page') is not False
    errors=set()
    if type(count) is not int or count<=0:errors.add('carousel_count_unverified')
    elif count!=len(children):errors.add('carousel_incomplete')
    if not children or len(children)>100 or next_page:errors.add('carousel_incomplete')
    identities=set();has_image=False
    for child in children[:100]:
        if not isinstance(child,dict):errors.add('carousel_incomplete');continue
        identity=child.get('pk',child.get('id'))
        valid_id=(type(identity) is int and identity>0 or isinstance(identity,str) and
                  re.fullmatch(r'[0-9]+(?:_[0-9]+)?',identity,re.ASCII) is not None)
        if not valid_id or str(identity) in identities:errors.add('carousel_incomplete')
        if valid_id:identities.add(str(identity))
        image=_images(child);has_image |= image
        if not image:errors.add('carousel_incomplete')
    return errors,has_image


class PartialCapture:
    def __init__(self,account,now=None):
        self._account=account.strip().lstrip('@').casefold()
        self._now=now or datetime.now(timezone.utc)
        self._key=os.urandom(32)
        self._seen=set();self._valid_seen=set()  # Ephemeral keyed digests, never emitted.
        self.counts={key:0 for key in COUNTERS}
        self.reasons={key:0 for key in REASONS}
        self._provenance_patterns=[]
        self._request_binding=None
        self._binding_counts={key:0 for key in binding.BINDINGS}
        self._binding_reasons={key:0 for key in binding.REASONS}

    @contextmanager
    def request_context(self,response):
        # Safe projection only; raw requests/variables never enter session state.
        previous=self._request_binding
        current=binding.classify(response,self._account)
        self._binding_counts[current['binding']]+=1
        self._binding_reasons[current['reason']]+=1
        self._request_binding=current
        try:yield
        finally:self._request_binding=previous

    def summary(self):
        return dict(status='partial_unverified',timeline_completeness='unverified',
                    production_eligible=False,image_availability='not_checked',
                    source_verification='payload_username_match_only',request_profile_binding='not_verified',
                    collaboration_semantics='unverified_hints_only',
                    request_binding_counts=dict(self._binding_counts),
                    request_binding_reason_counts=dict(self._binding_reasons),
                    candidate_provenance=[dict(pattern) for pattern in self._provenance_patterns],
                    counts=dict(self.counts),
                    incomplete_reasons=dict(self.reasons))

    def response_finished(self,before_responses,before_errors,health):
        if health['graphql_responses']>before_responses:
            self.counts['responses_inspected']+=1
            if health['capture_errors']>before_errors:
                self.counts['responses_rejected_by_production']+=1

    def observe(self,payload,category):
        try:self._observe(payload,category)
        except Exception:self.counts['diagnostic_errors']+=1

    def _observe(self,payload,category):
        if payload.get('errors'):self.counts['responses_with_graphql_errors']+=1
        if category=='ignored_graphql_response':return
        root=payload.get('data')
        if not isinstance(root,dict):return
        remaining=128;truncated=len(root)>64
        for key,connection in list(root.items())[:64]:
            if not isinstance(connection,dict) or not isinstance(connection.get('edges'),list):continue
            edges=connection['edges']
            known=(key in TIMELINE_ROOTS or any(marker in key.lower() for marker in ('timeline','profile_grid')))
            shaped=any(isinstance(edge,dict) and isinstance(edge.get('node'),dict) and
                       any(field in edge['node'] for field in ('code','media')) for edge in edges[:128])
            if not (known or shaped):continue
            connection_kind=('known_timeline_root' if key in TIMELINE_ROOTS else
                             'timeline_name_hint' if known else 'generic_media_edges')
            truncated |= len(edges)>remaining
            for edge in edges[:remaining]:
                remaining-=1
                if not isinstance(edge,dict) or 'node' not in edge:
                    self.counts['malformed_media_slots']+=1;continue
                node=edge['node']
                if node is None:self.counts['null_media_slots']+=1;continue
                if not isinstance(node,dict):self.counts['malformed_media_slots']+=1;continue
                wrapper_kind='flat_node'
                if 'code' in node:media=node
                elif 'media' in node:
                    wrapper_kind='node_media'
                    media=node['media']
                    if media is None:self.counts['null_media_slots']+=1;continue
                    if not isinstance(media,dict):self.counts['malformed_media_slots']+=1;continue
                elif 'media_or_ad' in node:
                    self.counts['unsupported_media_slots']+=1;continue
                elif known:media=node  # Missing identity must be counted as incomplete.
                else:self.counts['unsupported_media_slots']+=1;continue
                before_valid=self.counts['fully_valid_candidates']
                try:self._candidate(media,edge)
                except Exception:
                    self.counts['diagnostic_errors']+=1
                    self.counts['incomplete_candidates']+=1
                    self.reasons['validation_exception']+=1
                # Provenance is diagnostic only; never changes validation counts.
                try:
                    outcome='visible_fields_valid' if self.counts['fully_valid_candidates']>before_valid else 'incomplete'
                    pattern=_provenance(media,self._account,connection_kind,wrapper_kind,
                                        bool(payload.get('errors')),outcome)
                    current=self._request_binding or {}
                    pattern.update(request_binding=current.get('binding','unavailable'),
                        request_username_hint_relation=current.get('username_hint_relation','missing_or_invalid'),
                        numeric_target_hint=current.get('numeric_target_hint',False))
                    _add_pattern(self._provenance_patterns,pattern,PROVENANCE_FLAGS,PROVENANCE_ENUMS)
                except Exception:self.counts['diagnostic_errors']+=1
        if truncated:self.counts['inspection_truncated_responses']+=1

    def _candidate(self,media,edge):
        self.counts['candidate_media_nodes']+=1
        errors=set();digest=None
        code=media.get('code')
        if not isinstance(code,str) or not CODE.fullmatch(code):errors.add('invalid_identity')
        else:
            digest=hmac.new(self._key,code.encode(),hashlib.sha256).digest()
            if digest in self._seen:self.counts['duplicate_candidates']+=1
            elif len(self._seen)>=MAX_IDENTITIES:
                self.counts['diagnostic_errors']+=1
                errors.add('identity_tracking_limit')
            else:self._seen.add(digest);self.counts['unique_post_identities']+=1
        user=media.get('user')
        username=user.get('username') if isinstance(user,dict) else None
        if not isinstance(username,str) or not ACCOUNT.fullmatch(username):errors.add('source_account_missing')
        elif username.casefold()!=self._account:errors.add('source_account_mismatch')
        stamp=media.get('taken_at',media.get('device_timestamp'))
        try:
            if type(stamp) is not int or stamp<=0:raise ValueError()
            date=datetime.fromtimestamp(stamp,timezone.utc)
            if date>self._now+timedelta(minutes=5):raise ValueError()
        except (ValueError,OverflowError,OSError):errors.add('invalid_timestamp')
        if 'caption' not in media:errors.add('missing_caption')
        elif not isinstance(media['caption'],dict) or not isinstance(media['caption'].get('text'),str):
            errors.add('invalid_caption')
        carousel=(media.get('media_type')==8 or media.get('__typename')=='GraphSidecar' or
                  media.get('carousel_media') is not None or media.get('edge_sidecar_to_children') is not None)
        image=_images(media)
        if carousel:
            child_errors,child_image=_carousel(media)
            errors.update(child_errors);image |= child_image
        elif not (type(media.get('media_type')) is int and media['media_type'] in (1,2) or
                  media.get('__typename') in ('GraphImage','GraphVideo')):
            errors.add('media_type_unverified')
        if not image:errors.add('missing_image')
        if resolve_timeline_media(edge)[0] is None:errors.add('invalid_media_structure')
        if errors:
            self.counts['incomplete_candidates']+=1
            for reason in errors:self.reasons[reason]+=1
        else:
            self.counts['fully_valid_candidates']+=1
            if digest not in self._valid_seen:
                self._valid_seen.add(digest);self.counts['unique_fully_valid_candidates']+=1
