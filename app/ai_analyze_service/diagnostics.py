"""Run-local structured diagnostics. Never retain exception text, prompts or URLs."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

_current = ContextVar('instagram_analysis_diagnostics',default=None)
_PHASES={'input_preparation','image_retrieval','gemini_request','response_parsing_validation','database_persistence'}


def new_diagnostics():
    return dict(phase='input_preparation',failure_phase=None,exception_class=None,http_status=None,
                api_request_started=False,response_received=False,request_count=0,request_start_boundary='gemini_sdk_call',
                extraction_committed=False,selected_model=None,events=[])


@contextmanager
def capture(diagnostics):
    token=_current.set(diagnostics)
    try:yield diagnostics
    finally:_current.reset(token)


def phase(value):
    diagnostics=_current.get()
    if diagnostics is not None and value in _PHASES:diagnostics['phase']=value


def event(kind,exc=None,http_response=False,model=None):
    diagnostics=_current.get()
    if diagnostics is None:return
    item=dict(phase=diagnostics['phase'],event=kind)
    from app.ai_analyze_service.model_selection import diagnostic_model
    selected_model=diagnostic_model(model)
    if selected_model is not None:
        item['model']=selected_model
        if kind=='request_started':diagnostics['selected_model']=selected_model
    if exc is not None:
        name=type(exc).__name__
        item['exception_class']=name if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',name) else 'Exception'
        if kind == 'request_failed':
            status=getattr(exc,'status',None)
            if status in ('UNAVAILABLE','DEADLINE_EXCEEDED','RESOURCE_EXHAUSTED','INTERNAL','INVALID_ARGUMENT','NOT_FOUND','UNAUTHENTICATED','PERMISSION_DENIED'):
                item['provider_status']=status
            message=getattr(exc,'message','')
            if isinstance(message,str):
                item['overload_hint']=any(term in message.lower() for term in ('overload','high demand','capacity'))
                item['deadline_hint']=any(term in message.lower() for term in ('deadline','timed out','timeout'))
            response=getattr(exc,'response',None)
            item['http_response_present']=response is not None
            try:
                from urllib.parse import urlsplit
                host=urlsplit(str(response.request.url)).hostname
                item['official_api_host']=host=='generativelanguage.googleapis.com'
                item['json_response']='json' in response.headers.get('content-type','').lower()
            except Exception:pass
        for value in (getattr(exc,'code',None),getattr(exc,'status_code',None),getattr(getattr(exc,'response',None),'status_code',None)):
            if type(value) is int and 100<=value<=599:
                item['http_status']=value;break
    if kind=='request_started':
        diagnostics['api_request_started']=True;diagnostics['request_count']+=1
    if kind=='response_received' or http_response:diagnostics['response_received']=True
    if len(diagnostics['events'])<64:diagnostics['events'].append(item)
    if kind=='failure':
        diagnostics['failure_phase']=diagnostics['phase']
        diagnostics['exception_class']=item.get('exception_class')
        diagnostics['http_status']=item.get('http_status')
        if diagnostics['http_status'] is None:
            diagnostics['http_status']=next((e['http_status'] for e in reversed(diagnostics['events'])
                if e['phase']==diagnostics['phase'] and 'http_status' in e),None)


def committed():
    diagnostics=_current.get()
    if diagnostics is not None:diagnostics['extraction_committed']=True


def input_summary(image_bytes, prompt_chars):
    diagnostics=_current.get()
    if diagnostics is not None:
        diagnostics['input_summary']=dict(image_count=len(image_bytes),image_bytes=sum(image_bytes),largest_image_bytes=max(image_bytes,default=0),prompt_chars=prompt_chars)
