"""Diagnostic request hints, never proof of an opaque persisted query's target.

There is currently no reviewed Instagram operation/selector contract in this
repository. Consequently verified_* are reserved output categories, not outcomes
that can be inferred from variable names, operation names or payload authors.
Raw request data is inspected transiently and never returned or retained.
"""
import re
from urllib.parse import parse_qs

from app.instagram_scraper.query_transport import _json

BINDINGS=('verified_match','verified_mismatch','ambiguous','unavailable')
REASONS=('request_unavailable','body_unavailable','unsupported_encoding','malformed_metadata',
         'inspection_limit','variables_missing','variables_without_target_hint','unreviewed_target_selector')
RELATIONS=('matches_requested','different_account','conflicting','missing_or_invalid')
USERNAME=re.compile(r'[A-Za-z0-9._]{1,30}\Z',re.ASCII)


def classify(response, account):
    """Read only bounded, fixed-position POST variables; output fixed categories.

    Neither username nor numeric ID selectors prove which profile a private
    doc_id operation requests. In particular an ID cannot be mapped to the
    requested username using the response's media authors or coauthors.
    """
    result=dict(binding='unavailable',reason='request_unavailable',
                username_hint_relation='missing_or_invalid',numeric_target_hint=False)
    def finish(reason, binding='ambiguous'):
        result.update(reason=reason,binding=binding)
        return result
    try:
        request=response.request
        if request is None:return result
        if request.method!='POST':return finish('unsupported_encoding','unavailable')
        body=request.post_data
        if body is None or body=='':return finish('body_unavailable','unavailable')
        if not isinstance(body,str):return finish('unsupported_encoding')
        if len(body)>131072:return finish('inspection_limit')
        headers=request.headers
        mime=headers.get('content-type','') if isinstance(headers,dict) else ''
        if not isinstance(mime,str) or len(mime)>256:return finish('unsupported_encoding')
        mime=mime.split(';',1)[0].strip().lower()
        if mime=='application/json':
            data=_json(body)
        elif mime=='application/x-www-form-urlencoded':
            if re.search(r'%(?![0-9A-Fa-f]{2})',body):return finish('malformed_metadata')
            data=parse_qs(body,keep_blank_values=True,strict_parsing=True,
                          errors='strict',max_num_fields=128)
            if any(len(values)!=1 for values in data.values()):return finish('malformed_metadata')
            data={key:values[0] for key,values in data.items()}
        else:return finish('unsupported_encoding','unavailable')
        if not isinstance(data,dict):return finish('malformed_metadata')
        if 'variables' not in data:return finish('variables_missing','unavailable')
        variables=data['variables']
        if isinstance(variables,str):
            if len(variables)>65536:return finish('inspection_limit')
            variables=_json(variables)
        if not isinstance(variables,dict):return finish('malformed_metadata')
        if len(variables)>128:return finish('inspection_limit')
        # These spellings are hints only, not a reviewed GraphQL contract. Do
        # not recurse through arbitrary inputs, lists or candidate response data.
        names=[variables[key] for key in ('username','target_username','profile_username')
               if key in variables]
        valid=[name.casefold() for name in names if isinstance(name,str) and USERNAME.fullmatch(name)]
        if names and len(valid)==len(names):
            result['username_hint_relation']=('conflicting' if len(set(valid))>1 else
                'matches_requested' if valid[0]==account.casefold() else 'different_account')
        for key in ('id','user_id','target_user_id','profile_id'):
            value=variables.get(key)
            if (type(value) is int and value>0 or isinstance(value,str) and
                    re.fullmatch(r'[0-9]{1,32}',value,re.ASCII)):
                result['numeric_target_hint']=True
        return finish('unreviewed_target_selector' if names or result['numeric_target_hint']
                      else 'variables_without_target_hint')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return finish('malformed_metadata')
    except Exception:
        return finish('request_unavailable','unavailable')
