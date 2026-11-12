"""Bounded request-shape inspection. Only the transient query may leave this helper.

The second return value is a fixed privacy projection: no body, URL, header values,
operation names, document IDs, hashes, variable values or arbitrary keys.
"""
import json
import re
from urllib.parse import parse_qs

TRANSPORTS=('inline_query','persisted_doc_id','persisted_hash','persisted_mixed',
    'operation_only','no_query_metadata','query_invalid','query_ambiguous',
    'request_unavailable','request_access_error','body_access_error','body_unavailable',
    'body_too_large','unsupported_encoding','malformed_body','json_non_object','batched_json_unsupported')
ENCODINGS=('json','form','other','none','unknown')
METHODS=('POST','GET','other','unknown')
CONTENT_TYPES=('json','form','graphql','multipart','other','unknown')
FLAGS=('request_available','body_present','has_query_field','has_inline_query','has_doc_id',
       'has_persisted_query_hash','has_extensions','extensions_invalid','has_operation_name',
       'has_friendly_operation_name')


def new_counts():
    return dict(responses=0,transport_counts={key:0 for key in TRANSPORTS},
                encoding_counts={key:0 for key in ENCODINGS},method_counts={key:0 for key in METHODS},
                content_type_counts={key:0 for key in CONTENT_TYPES},indicator_counts={key:0 for key in FLAGS})


def add_observation(counts,details):
    counts['responses']+=1
    for key,allowed in (('transport',TRANSPORTS),('encoding',ENCODINGS),
                        ('method',METHODS),('content_type',CONTENT_TYPES)):
        value=details.get(key)
        if value not in allowed:value=allowed[-1]
        counts[key+'_counts'][value]+=1
    for key in FLAGS:counts['indicator_counts'][key]+=int(details.get(key) is True)


def _json(body):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError()
            result[key]=value
        return result
    return json.loads(body,object_pairs_hook=unique)


def inspect_request(response):
    details=dict(transport='request_unavailable',encoding='unknown',method='unknown',
                 content_type='unknown',**{key:False for key in FLAGS})
    def finish(category,query=None):
        details['transport']=category
        return query,details
    try:request=response.request
    except Exception:return finish('request_access_error')
    if request is None:return finish('request_unavailable')
    details['request_available']=True
    try:
        method=request.method
        if isinstance(method,str):details['method']=method if method in ('POST','GET') else 'other'
    except Exception:pass
    try:
        headers=request.headers
        value=headers.get('content-type') if isinstance(headers,dict) else None
        if isinstance(value,str) and len(value)<=256:
            mime=value.split(';',1)[0].strip().lower()
            details['content_type']={'application/json':'json',
                'application/x-www-form-urlencoded':'form','application/graphql':'graphql',
                'multipart/form-data':'multipart'}.get(mime,'other')
    except Exception:pass
    try:body=request.post_data
    except UnicodeError:return finish('unsupported_encoding')
    except Exception:return finish('body_access_error')
    if body is None or body=='':
        details['encoding']='none'
        return finish('body_unavailable')
    details['body_present']=True
    if not isinstance(body,str):return finish('unsupported_encoding')
    if len(body)>131072:return finish('body_too_large')
    content_type=details['content_type']
    if content_type in ('graphql','multipart'):
        details['encoding']='other'
        return finish('unsupported_encoding')
    form=False
    try:
        if content_type=='form':raise ValueError()
        value=_json(body)
        details['encoding']='json'
    except (ValueError,TypeError,RecursionError):
        if content_type=='json' or body.lstrip().startswith(('{','[')):
            details['encoding']='json'
            return finish('malformed_body')
        if '=' not in body:
            details['encoding']='other'
            return finish('unsupported_encoding')
        details['encoding']='form';form=True
        try:
            if re.search(r'%(?![0-9A-Fa-f]{2})',body):raise ValueError()
            value=parse_qs(body,keep_blank_values=True,strict_parsing=True,
                           errors='strict',max_num_fields=128)
        except (ValueError,UnicodeError):return finish('malformed_body')
    if isinstance(value,list):return finish('batched_json_unsupported')
    if not isinstance(value,dict):return finish('json_non_object')
    for key,flag in (('query','has_query_field'),('doc_id','has_doc_id'),
                    ('operationName','has_operation_name'),
                    ('fb_api_req_friendly_name','has_friendly_operation_name'),
                    ('extensions','has_extensions')):
        details[flag]=key in value
    details['has_persisted_query_hash']=any(key in value for key in ('query_hash','queryHash'))
    extensions=value.get('extensions')
    if form and extensions is not None:
        if len(extensions)==1 and len(extensions[0])<=16384:
            try:extensions=_json(extensions[0])
            except (ValueError,TypeError,RecursionError):extensions=None
        else:extensions=None
    if details['has_extensions']:
        details['extensions_invalid']=not isinstance(extensions,dict)
        if isinstance(extensions,dict):
            persisted=extensions.get('persistedQuery')
            details['has_persisted_query_hash'] |= isinstance(persisted,dict) and 'sha256Hash' in persisted
    query=value.get('query')
    if details['has_query_field']:
        if form:
            if len(query)!=1:return finish('query_ambiguous')
            query=query[0]
        if not isinstance(query,str) or not query.strip():return finish('query_invalid')
        details['has_inline_query']=True
        return finish('inline_query',query)
    if details['has_doc_id'] and details['has_persisted_query_hash']:return finish('persisted_mixed')
    if details['has_doc_id']:return finish('persisted_doc_id')
    if details['has_persisted_query_hash']:return finish('persisted_hash')
    if details['has_operation_name'] or details['has_friendly_operation_name']:return finish('operation_only')
    return finish('no_query_metadata')
