"""Opt-in, in-memory schema review. Never retain payloads, paths, queries or messages."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from app.instagram_scraper.query_transport import inspect_request, new_counts, add_observation

from app.instagram_scraper.graphql_diagnostics import classify_error_path, TIMELINE_ROOTS

_current=ContextVar('instagram_schema_probe',default=None)
CATEGORIES=('observed','known_field','malformed_or_unrecognized_path','query_unavailable',
            'unsupported_query','alias_rejected','query_path_mismatch','unsafe_identifier',
            'static_candidate','candidate_limit','inspection_error','omitted_errors')
SAFE_NAME=re.compile(r'[a-z]+(?:_[a-z]+){0,7}\Z',re.ASCII)
SENSITIVE_PARTS=frozenset(('token','secret','password','passwd','cookie','authorization',
                           'credential','email','phone','username','session','key','url','uri',
                           'private','account','auth','bearer','access','refresh','csrf','jwt'))
TOKEN=re.compile(r'\s+|#[^\n]*|"(?:[^"\\\x00-\x1f]|\\.)*"|[A-Za-z_][A-Za-z_0-9]*|-?[0-9]+(?:\.[0-9]+)?|[!$():=@\[\]{},]')


class UnsupportedQuery(Exception):pass


def selections(document):
    """Parse a bounded selection-only GraphQL subset; reject fragments/directives.

    This is intentionally not a general GraphQL parser. Unsupported syntax is
    never interpreted heuristically. Query contents remain transient.
    """
    if not isinstance(document,str) or len(document)>65536:raise UnsupportedQuery()
    tokens=[];offset=0
    while offset<len(document):
        match=TOKEN.match(document,offset)
        if not match:raise UnsupportedQuery()
        token=match.group();offset=match.end()
        if not token.isspace() and not token.startswith('#') and token!=',':tokens.append(token)
        if len(tokens)>8192:raise UnsupportedQuery()
    position=0
    def peek():return tokens[position] if position<len(tokens) else None
    def take():
        nonlocal position
        value=peek()
        if value is None:raise UnsupportedQuery()
        position+=1;return value
    def name():
        value=take()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',value,re.ASCII):raise UnsupportedQuery()
        return value
    def arguments():
        if peek()!='(':return
        take();stack=[')']
        pairs={'(':')','[':']','{':'}'}
        while stack:
            value=take()
            if value in pairs:
                stack.append(pairs[value])
                if len(stack)>24:raise UnsupportedQuery()
            elif value in (')',']','}'):
                if value!=stack.pop():raise UnsupportedQuery()
    def selection(depth):
        if depth>24 or take()!='{':raise UnsupportedQuery()
        fields={}
        while peek()!='}':
            response_name=name();field=response_name;aliased=False
            if peek()==':':take();field=name();aliased=True
            arguments()
            if peek()=='@':raise UnsupportedQuery()
            children=selection(depth+1) if peek()=='{' else {}
            if response_name in fields:raise UnsupportedQuery()
            fields[response_name]=(field,aliased,children)
        take()
        if not fields:raise UnsupportedQuery()
        return fields
    if peek()=='query':
        take()
        if peek() not in ('{','('):name()
        arguments()
    result=selection(0)
    if peek() is not None:raise UnsupportedQuery()
    return result


def _target(path):
    info=classify_error_path(path)
    if info['category'] in ('malformed_path','missing_path','unknown') or not info['known_timeline_root']:
        return None
    if info['first_media_field']!='unmatched':return 'known'
    offset=1 if path[0] in TIMELINE_ROOTS else 2
    if path[offset:offset+1]!=['edges'] or len(path)<offset+4 or path[offset+2]!='node':return None
    index=offset+3;placement='flat'
    if path[index:index+2]==['media_or_ad','media']:index+=2;placement='media_or_ad_media'
    elif path[index:index+1] in (['media'],['media_or_ad']):
        placement=path[index];index+=1
    if index>=len(path) or not isinstance(path[index],str):return None
    return path[:index+1],path[index],placement


def _query(response,record_transport=None):
    query,details=inspect_request(response)
    if record_transport is not None:record_transport(details)
    return query


class Probe:
    def __init__(self):
        self.counts={key:0 for key in CATEGORIES}
        self.transport_diagnostics=new_counts()
        self._candidates={}  # Only validated field labels, placement enum and count.

    def summary(self):
        return dict(schema_probe='enabled',counts=dict(self.counts),candidate_count=len(self._candidates),
                    query_transport=self._transport_summary())

    def _transport_summary(self):
        return {key:dict(value) if isinstance(value,dict) else value
                for key,value in self.transport_diagnostics.items()}

    def _record_transport(self,details):
        add_observation(self.transport_diagnostics,details)

    def observe(self,response,payload):
        try:self._observe(response,payload)
        except Exception:self.counts['inspection_error']+=1  # No exception messages.

    def _observe(self,response,payload):
        errors=payload.get('errors')
        items=errors if isinstance(errors,list) else [errors]
        self.counts['omitted_errors']+=max(0,len(items)-128)
        document=None;parsed=False;query_status=None
        for item in items[:128]:
            self.counts['observed']+=1
            target=_target(item.get('path') if isinstance(item,dict) else None)
            if target is None:self.counts['malformed_or_unrecognized_path']+=1;continue
            if target=='known':self.counts['known_field']+=1;continue
            prefix,field,placement=target
            if (not (3<=len(field)<=48 and SAFE_NAME.fullmatch(field)) or
                    set(field.split('_')) & SENSITIVE_PARTS or
                    re.fullmatch(r'[a-f]{8,}',field) or ('_' not in field and len(field)>16)):
                self.counts['unsafe_identifier']+=1;continue
            if not parsed:
                parsed=True
                query=_query(response,self._record_transport)
                if not isinstance(query,str):query_status='query_unavailable'
                else:
                    try:document=selections(query)
                    except UnsupportedQuery:query_status='unsupported_query'
            if query_status:self.counts[query_status]+=1;continue
            current=document;failure=None
            for segment in prefix:
                if type(segment) is int:continue
                entry=current.get(segment)
                if entry is None:failure='query_path_mismatch';break
                if entry[1]:failure='alias_rejected';break
                current=entry[2]
            if failure:self.counts[failure]+=1;continue
            key=(field,placement)
            if key not in self._candidates and len(self._candidates)>=8:
                self.counts['candidate_limit']+=1;continue
            self._candidates[key]=self._candidates.get(key,0)+1
            self.counts['static_candidate']+=1

    def review(self,confirm):
        """Only a caller's explicit human confirmation can release a field label."""
        reviewed=[]
        for (field,placement),count in sorted(self._candidates.items()):
            if confirm(field,placement) is True:
                reviewed.append(dict(field=field,placement=placement,observations=count,
                                     evidence='unaliased_query_selection'))
        return dict(format='instagram_schema_review_v1',reviewed_fields=reviewed,
                    counts=dict(self.counts),query_transport=self._transport_summary())


@contextmanager
def enabled(probe):
    token=_current.set(probe)
    try:yield
    finally:_current.reset(token)


def observe(response,payload):
    probe=_current.get()
    if probe is not None:probe.observe(response,payload)


def capture_observer():
    """Bind before Playwright dispatches callbacks in its separate event greenlet."""
    probe=_current.get()
    return probe.observe if probe is not None else None
