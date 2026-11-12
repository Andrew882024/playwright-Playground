"""Privacy projection only. This module never decides whether to accept a timeline."""

from app.instagram_scraper.timeline_media import resolve_timeline_media, RESOLUTION_KINDS

# Explicit diagnostic values, not a range and not a claim about their meaning.
# Unknown numeric codes and all string codes are represented by booleans only.
SAFE_ERROR_CODES = frozenset((1,2,4,10,17,32,100,102,104,190,200,341,368,613,
                             1357001,1357004,1675004,1675030))
CODE_FIELDS = ('code','error_code','api_error_code')
ERROR_FIELDS = ('message','summary','description','path','locations','extensions',
                'code','error_code','api_error_code','error_subcode','error_data','is_transient','severity')
ERROR_FLAGS = tuple('has_'+key for key in ERROR_FIELDS) + (
    'has_extensions_code','has_extensions_error_code','has_extensions_api_error_code',
    'has_extensions_error_subcode','has_unallowlisted_numeric_code','has_non_numeric_code')
SCHEMA_FLAGS = ('data_present','data_is_object','edges_present','has_list_edges','has_non_list_edges',
                'nodes_nested','node_has_code','node_has_shortcode','node_has_id','pagination_present',
                'inspection_truncated')
SCHEMA_KINDS = ('top_level_edges','nested_edges','alternate_nodes','post_like_other','unknown')
PAGINATION_FIELDS = frozenset(('page_info','has_next_page','has_previous_page','end_cursor','start_cursor',
                               'next_max_id','more_available','pageInfo','hasNextPage','hasPreviousPage',
                               'endCursor','startCursor','next_cursor','nextCursor'))
TIMELINE_ROOTS = frozenset(('timeline_connection','profile_grid','edge_owner_to_timeline_media',
    'xdt_api__v1__feed__user_timeline_graphql_connection'))
OPTIONAL_ROOT_CANDIDATES = frozenset(('inbox','notifications','suggested_users'))
# The scraper reads comment counts/previews, so those are intentionally NOT optional.
OPTIONAL_NODE_FIELDS = frozenset(('like_count','has_liked','viewer_has_liked','viewer_has_saved'))
REQUIRED_PATH_FIELDS = {
    'timeline_identity':frozenset(('code','shortcode','id','pk')),
    'timeline_time':frozenset(('taken_at','taken_at_timestamp','timestamp','device_timestamp')),
    'timeline_media':frozenset(('image_versions2','image_versions','display_url','thumbnail_src',
                              'carousel_media','video_versions','video_url',
                              'display_resources','edge_sidecar_to_children')),
    'timeline_caption':frozenset(('caption','accessibility_caption','headline')),
    'timeline_organizer':frozenset(('user','owner')),
    'timeline_comments':frozenset(('edge_media_to_comment','preview_comments','comment_count')),
    'timeline_pinning':frozenset(('is_pinned','is_pinned_to_profile')),
}
# Reviewed against sync_ins._post_media_cdn_urls_from_timeline_node,
# _post_title/_post_caption_text, _post_timestamp_unix, _from_username,
# _raw_comment_nodes_from_media and timeline_media._valid_media.
# Existing legacy diagnostic names and optional candidates are retained; this
# vocabulary does not assert that every listed field is required for extraction.
# Arbitrary keys visited by the CDN fallback are deliberately NOT allowlisted.
FIRST_MEDIA_FIELDS = tuple(sorted(set().union(*REQUIRED_PATH_FIELDS.values(), OPTIONAL_NODE_FIELDS)))
FIRST_MEDIA_FIELD_ENUMS = (*FIRST_MEDIA_FIELDS, 'unmatched')
PATH_KINDS = ('timeline_identity','timeline_time','timeline_media','timeline_caption','timeline_organizer',
    'timeline_comments','timeline_pinning','timeline_pagination','timeline_connection',
    'timeline_edge','timeline_node','timeline_wrapper','timeline_unknown_field','optional_node_field_candidate',
    'optional_root_candidate','missing_path','malformed_path','unknown')
PATH_FLAGS = ('path_present','path_is_list','has_indices','inspection_truncated',
              'known_timeline_root','known_optional_root','contains_unknown_segments',
              'unknown_at_connection','unknown_at_node','unknown_below_required_field',
              'ends_at_edge','ends_at_node','ends_at_media_wrapper')
WRAPPER_FLAGS = ('candidate_is_null','candidate_is_list','candidate_is_scalar','candidate_is_object','has_code','code_is_nonempty_string','has_shortcode',
    'has_id','has_pk','has_taken_at','has_taken_at_timestamp','has_caption','has_user',
    'has_image_versions2','has_display_url','has_carousel_media','has_video_versions',
    'has_is_pinned','has_is_pinned_to_profile',
    'has_media_child','has_media_or_ad_child','has_items_child','has_nodes_child')
WRAPPER_KINDS = ('flat_node','node_media','node_media_or_ad','node_media_or_ad_media',
    'empty_edges','missing_edges','non_list_edges','missing_node','non_object_node','non_object_edge','unknown')
PLACEMENTS = ('top_level','under_user','unknown')
_MISSING = object()
ROOT_KINDS = ('timeline_known','other_or_unknown')
CORRELATION_COUNTS = ('responses','with_recognized_media','with_flat_media','with_nested_media',
    'with_unresolved_nodes','with_null_placeholders','with_malformed_nodes','with_unsupported_nodes',
    'with_critical_paths','with_unknown_paths','with_optional_candidate_paths')


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def new_diagnostics():
    return dict(graphql_error_diagnostics=dict(responses=0,error_objects=0,omitted_error_items=0,
        code_counts={str(code):0 for code in sorted(SAFE_ERROR_CODES)},
        first_media_field_counts={field:0 for field in FIRST_MEDIA_FIELD_ENUMS},
        structures=[],path_classifications=[]),
        graphql_error_timeline_correlation={key:0 for key in CORRELATION_COUNTS},
        unsupported_timeline_shapes=[],timeline_wrapper_diagnostics=dict(
            responses=0,inspected_edges=0,omitted_items=0,patterns=[]))


def classify_error_path(path, *, present=True):
    """Attribution only; candidates are never proof that partial data is safe to use."""
    flags={key:False for key in PATH_FLAGS};flags['path_present']=present
    first_field='unmatched'
    def result(category):return dict(flags,category=category,first_media_field=first_field)
    if not present:return result('missing_path')
    if not isinstance(path,list) or not path:return result('malformed_path')
    flags['path_is_list']=True
    flags['has_indices']=any(type(token) is int for token in path[:32])
    if len(path)>32:
        flags['inspection_truncated']=True
        return result('unknown')
    if any(not isinstance(token,str) and not (type(token) is int and token>=0) for token in path):
        return result('malformed_path')
    known=set(TIMELINE_ROOTS)|set(OPTIONAL_ROOT_CANDIDATES)|set(OPTIONAL_NODE_FIELDS)|set(PAGINATION_FIELDS)
    known.update(('edges','node','media','media_or_ad','text','candidates','url','src',
                  'width','height','username','count','created_at'))
    for fields in REQUIRED_PATH_FIELDS.values():known.update(fields)
    flags['contains_unknown_segments']=any(isinstance(token,str) and token not in known for token in path)
    offset=1
    if isinstance(path[0],str) and path[0] in TIMELINE_ROOTS:
        flags['known_timeline_root']=True
    elif len(path)>1 and path[0]=='user' and isinstance(path[1],str) and path[1] in TIMELINE_ROOTS:
        flags['known_timeline_root']=True;offset=2
    elif isinstance(path[0],str) and path[0] in OPTIONAL_ROOT_CANDIDATES:
        flags['known_optional_root']=True
        return result('unknown' if flags['contains_unknown_segments'] else 'optional_root_candidate')
    else:return result('unknown')
    tail=path[offset:]
    if not tail:return result('timeline_connection')
    if isinstance(tail[0],str) and tail[0] in PAGINATION_FIELDS:
        # The ancestor is known even if a descendant cannot be attributed.
        flags['unknown_below_required_field']=flags['contains_unknown_segments']
        return result('timeline_pagination')
    if len(tail)==2 and tail[0]=='edges' and type(tail[1]) is int:
        flags['ends_at_edge']=True
        return result('timeline_edge')
    if len(tail)<3 or tail[0]!='edges' or type(tail[1]) is not int or tail[2]!='node':
        flags['unknown_at_connection']=True
        return result('timeline_unknown_field')
    tail=tail[3:]
    if not tail:
        flags['ends_at_node']=True
        return result('timeline_node')
    wrapped=False
    if tail[:2]==['media_or_ad','media']:tail=tail[2:];wrapped=True
    elif tail[:1] in (['media'],['media_or_ad']):tail=tail[1:];wrapped=True
    if wrapped and not tail:
        flags['ends_at_media_wrapper']=True
        return result('timeline_wrapper')
    if isinstance(tail[0],str) and tail[0] in FIRST_MEDIA_FIELDS:
        first_field=tail[0]
    if len(tail)==1 and isinstance(tail[0],str) and tail[0] in OPTIONAL_NODE_FIELDS:
        return result('optional_node_field_candidate')
    # A known consumed-field ancestor is evidence of its scope, not proof that
    # an unknown descendant is safe. Preserve uncertainty independently.
    for category,fields in REQUIRED_PATH_FIELDS.items():
        if isinstance(tail[0],str) and tail[0] in fields:
            flags['unknown_below_required_field']=flags['contains_unknown_segments']
            return result(category)
    flags['unknown_at_node']=True
    return result('timeline_unknown_field')


def _add_pattern(target, pattern, flags, enums):
    """Re-project at every boundary; never copy unknown keys or values."""
    if not isinstance(pattern,dict) or not _count(pattern.get('count')):return
    clean={key:pattern.get(key) is True for key in flags}
    for key,allowed in enums.items():
        value=pattern.get(key)
        clean[key]=value if isinstance(value,str) and value in allowed else allowed[-1]
    for existing in target:
        if all(existing[key]==value for key,value in clean.items()):
            existing['count']+=pattern['count'];return
    target.append(dict(clean,count=pattern['count']))


def wrapper_diagnostics(payload):
    """Inspect fixed positions only. These are candidate shapes, never extracted posts."""
    result=dict(responses=1,inspected_edges=0,omitted_items=0,patterns=[])
    root=payload.get('data') if isinstance(payload,dict) else None
    connections=[]
    if isinstance(root,dict):
        for placement,container in (('top_level',root),('under_user',root.get('user'))):
            if not isinstance(container,dict):continue
            for key,value in container.items():
                if key in TIMELINE_ROOTS or (isinstance(value,dict) and any(k in value for k in ('edges','items','nodes'))):
                    connections.append((placement,'timeline_known' if key in TIMELINE_ROOTS else 'other_or_unknown',value))
    result['omitted_items']+=max(0,len(connections)-64)
    def add(kind,placement='unknown',root_kind='other_or_unknown',candidate=_MISSING):
        flags={key:False for key in WRAPPER_FLAGS}
        flags['candidate_is_null']=candidate is None
        flags['candidate_is_list']=isinstance(candidate,list)
        flags['candidate_is_scalar']=candidate is not _MISSING and candidate is not None and not isinstance(candidate,(dict,list))
        if isinstance(candidate,dict):
            flags['candidate_is_object']=True
            for key in ('code','shortcode','id','pk','taken_at','taken_at_timestamp','caption','user',
                        'image_versions2','display_url','carousel_media','video_versions','is_pinned','is_pinned_to_profile'):
                flags['has_'+key]=key in candidate
            for key in ('media','media_or_ad','items','nodes'):flags['has_'+key+'_child']=key in candidate
            flags['code_is_nonempty_string']=isinstance(candidate.get('code'),str) and bool(candidate['code'].strip())
        _add_pattern(result['patterns'],dict(flags,wrapper_kind=kind,connection_placement=placement,
                     root_kind=root_kind,count=1),WRAPPER_FLAGS,
                     dict(wrapper_kind=WRAPPER_KINDS,connection_placement=PLACEMENTS,root_kind=ROOT_KINDS))
    if not connections:add('unknown')
    for placement,root_kind,connection in connections[:64]:
        if not isinstance(connection,dict):add('unknown',placement,root_kind);continue
        if 'edges' not in connection:add('missing_edges',placement,root_kind);continue
        edges=connection['edges']
        if not isinstance(edges,list):add('non_list_edges',placement,root_kind);continue
        if not edges:add('empty_edges',placement,root_kind);continue
        remaining=max(0,128-result['inspected_edges'])
        result['omitted_items']+=max(0,len(edges)-remaining)
        for edge in edges[:remaining]:
            result['inspected_edges']+=1
            if not isinstance(edge,dict):add('non_object_edge',placement,root_kind);continue
            if 'node' not in edge:add('missing_node',placement,root_kind);continue
            node=edge['node']
            if not isinstance(node,dict):add('non_object_node',placement,root_kind,node);continue
            add('flat_node',placement,root_kind,node)
            if 'media' in node:add('node_media',placement,root_kind,node['media'])
            if 'media_or_ad' in node:
                wrapped=node['media_or_ad'];add('node_media_or_ad',placement,root_kind,wrapped)
                if isinstance(wrapped,dict) and 'media' in wrapped:
                    add('node_media_or_ad_media',placement,root_kind,wrapped['media'])
    return result


def record_error_response(health, payload):
    """Inspect errored data without publishing it; correlate only within this response."""
    wrappers=wrapper_diagnostics(payload)
    combined=aggregate_diagnostics([health,dict(timeline_wrapper_diagnostics=wrappers)])
    health['timeline_wrapper_diagnostics']=combined['timeline_wrapper_diagnostics']
    counts=health['graphql_error_timeline_correlation']
    counts['responses']+=1
    outcomes=set()
    root=payload.get('data')
    if isinstance(root,dict):
        # Same top-level extraction boundary, with bounded diagnostic inspection.
        for connection in list(root.values())[:64]:
            if isinstance(connection,dict) and isinstance(connection.get('edges'),list):
                for edge in connection['edges'][:128]:
                    outcomes.add(resolve_timeline_media(edge)[1])
    mapping={'flat_media':'with_flat_media','nested_media':'with_nested_media',
             'null_placeholder':'with_null_placeholders','malformed':'with_malformed_nodes',
             'unsupported':'with_unsupported_nodes'}
    for outcome in outcomes:counts[mapping[outcome]]+=1
    if outcomes & {'flat_media','nested_media'}:counts['with_recognized_media']+=1
    if outcomes & {'null_placeholder','malformed','unsupported'}:counts['with_unresolved_nodes']+=1
    errors=payload.get('errors')
    items=errors if isinstance(errors,list) else [errors]
    paths=[classify_error_path(item.get('path'),present='path' in item)
           for item in items[:128] if isinstance(item,dict)]
    categories={item['category'] for item in paths}
    if categories & (set(REQUIRED_PATH_FIELDS) | {'timeline_connection','timeline_pagination',
                                                        'timeline_edge','timeline_node','timeline_wrapper'}):
        counts['with_critical_paths']+=1
    if (categories & {'unknown','timeline_unknown_field','missing_path','malformed_path'} or
            any(item['contains_unknown_segments'] or item['inspection_truncated'] for item in paths)):
        counts['with_unknown_paths']+=1
    if categories & {'optional_node_field_candidate','optional_root_candidate'}:
        counts['with_optional_candidate_paths']+=1


def record_errors(health, errors):
    details=health['graphql_error_diagnostics'];details['responses']+=1
    container='list' if isinstance(errors,list) else 'object' if isinstance(errors,dict) else 'other'
    items=errors if isinstance(errors,list) else [errors]
    details['omitted_error_items']+=max(0,len(items)-128)
    for item in items[:128]:
        flags={key:False for key in ERROR_FLAGS}
        codes=set()
        if isinstance(item,dict):
            details['error_objects']+=1
            flags.update({'has_'+key:key in item for key in ERROR_FIELDS})
            extensions=item.get('extensions')
            if isinstance(extensions,dict):
                flags.update({'has_extensions_'+key:key in extensions for key in (*CODE_FIELDS,'error_subcode')})
            for obj in (item,extensions):
                if not isinstance(obj,dict):continue
                for key in CODE_FIELDS:
                    if key not in obj:continue
                    value=obj[key]
                    if type(value) is int:
                        if value in SAFE_ERROR_CODES:codes.add(value)
                        else:flags['has_unallowlisted_numeric_code']=True
                    else:flags['has_non_numeric_code']=True
        for code in codes:details['code_counts'][str(code)]+=1
        path=classify_error_path(item.get('path') if isinstance(item,dict) else None,
                                 present=isinstance(item,dict) and 'path' in item)
        details['first_media_field_counts'][path['first_media_field']]+=1
        _add_pattern(details['path_classifications'],dict(path,count=1),PATH_FLAGS,dict(category=PATH_KINDS))
        _add_pattern(details['structures'],dict(flags,container=container,
            item_kind='object' if isinstance(item,dict) else 'other',count=1),ERROR_FLAGS,
            dict(container=('list','object','other'),item_kind=('object','other')))


def schema_shape(payload):
    """Walk fixed structural markers; never return keys, values, paths or cursors."""
    shape={key:False for key in SCHEMA_FLAGS}
    shape['data_present']=isinstance(payload,dict) and 'data' in payload
    root=payload.get('data') if isinstance(payload,dict) else None
    shape['data_is_object']=isinstance(root,dict)
    stack=[(root,(),False)];visited=0;top_edges=False;nested_edges=False;alternate=False
    while stack:
        value,path,in_node=stack.pop();visited+=1
        if visited>4096 or len(path)>24:
            shape['inspection_truncated']=True
            if visited>4096:break
            continue
        if isinstance(value,dict):
            if in_node:
                for key in ('code','shortcode','id'):
                    shape['node_has_'+key] |= key in value
            for key,child in value.items():
                if key in PAGINATION_FIELDS:shape['pagination_present']=True
                if key=='edges':
                    shape['edges_present']=True
                    shape['has_list_edges'] |= isinstance(child,list)
                    shape['has_non_list_edges'] |= not isinstance(child,list)
                    top_edges |= len(path)==1
                    nested_edges |= len(path)>1
                node_container=key in ('node','nodes','items','media')
                if key in ('nodes','items'):alternate=True
                if node_container:
                    canonical=key=='node' and len(path)==3 and path[-2:]==('edges','[]')
                    if not canonical:shape['nodes_nested']=True
                stack.append((child,path+(key,),node_container))
        elif isinstance(value,list):
            stack.extend((child,path+('[]',),in_node) for child in value)
    shape['shape_kind']=('nested_edges' if nested_edges else 'top_level_edges' if top_edges else
                         'alternate_nodes' if alternate else 'post_like_other' if
                         any(shape['node_has_'+key] for key in ('code','shortcode','id')) else 'unknown')
    return shape


def record_unsupported(health,payload):
    _add_pattern(health['unsupported_timeline_shapes'],dict(schema_shape(payload),count=1),
                 SCHEMA_FLAGS,dict(shape_kind=SCHEMA_KINDS))
    combined=aggregate_diagnostics([health,dict(timeline_wrapper_diagnostics=wrapper_diagnostics(payload))])
    health['timeline_wrapper_diagnostics']=combined['timeline_wrapper_diagnostics']


def aggregate_diagnostics(summaries):
    """Used by scraper JSON and durable logs; numeric/enum/boolean allowlist only."""
    result=new_diagnostics();target=result['graphql_error_diagnostics']
    for raw in summaries:
        if not isinstance(raw,dict):continue
        correlation=raw.get('graphql_error_timeline_correlation',{})
        if isinstance(correlation,dict):
            for key in CORRELATION_COUNTS:
                result['graphql_error_timeline_correlation'][key]+=_count(correlation.get(key))
        details=raw.get('graphql_error_diagnostics',{})
        if isinstance(details,dict):
            for key in ('responses','error_objects','omitted_error_items'):target[key]+=_count(details.get(key))
            codes=details.get('code_counts',{})
            if isinstance(codes,dict):
                for key in target['code_counts']:target['code_counts'][key]+=_count(codes.get(key))
            fields=details.get('first_media_field_counts',{})
            if isinstance(fields,dict):
                for key in FIRST_MEDIA_FIELD_ENUMS:
                    target['first_media_field_counts'][key]+=_count(fields.get(key))
            patterns=details.get('structures',[])
            if isinstance(patterns,list):
                for item in patterns:_add_pattern(target['structures'],item,ERROR_FLAGS,
                    dict(container=('list','object','other'),item_kind=('object','other')))
            patterns=details.get('path_classifications',[])
            if isinstance(patterns,list):
                for item in patterns:_add_pattern(target['path_classifications'],item,PATH_FLAGS,dict(category=PATH_KINDS))
        patterns=raw.get('unsupported_timeline_shapes',[])
        if isinstance(patterns,list):
            for item in patterns:_add_pattern(result['unsupported_timeline_shapes'],item,SCHEMA_FLAGS,
                                              dict(shape_kind=SCHEMA_KINDS))
        wrappers=raw.get('timeline_wrapper_diagnostics',{})
        if isinstance(wrappers,dict):
            combined=result['timeline_wrapper_diagnostics']
            for key in ('responses','inspected_edges','omitted_items'):combined[key]+=_count(wrappers.get(key))
            patterns=wrappers.get('patterns',[])
            if isinstance(patterns,list):
                for item in patterns:_add_pattern(combined['patterns'],item,WRAPPER_FLAGS,
                    dict(wrapper_kind=WRAPPER_KINDS,connection_placement=PLACEMENTS,root_kind=ROOT_KINDS))
    return result
