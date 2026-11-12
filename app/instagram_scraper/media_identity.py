"""Numeric media identity, distinct from case-sensitive shortcode identity.

Reuse the existing collaboration numeric-ID validator. Compound/opaque node IDs
are classified, never split, decoded or assumed to alias pk. Diagnostics contain
only fixed enums, never identifier values.
"""
import re
from app.instagram_collaboration import _media_id

SHAPES=('absent','null','empty','numeric','compound_numeric','opaque_string','invalid_number','invalid_type')
RELATIONS=('missing','incomplete','unsupported_layout','invalid','conflicting','consistent','single_numeric')
FAILURES=frozenset(('missing_media_identity','incomplete_media_identity','unsupported_media_identity_layout',
                    'invalid_media_identity','conflicting_media_identity'))


def numeric_id(value):
    checked=_media_id(value)
    # Numeric aliases can differ only in representation (int/string/leading zero).
    # Shortcodes never enter this function and are never lowercased or decoded.
    return str(int(checked)) if checked is not None else None


def _shape(media,key):
    if key not in media:return 'absent'
    value=media[key]
    if value is None:return 'null'
    if numeric_id(value) is not None:return 'numeric'
    if isinstance(value,str):
        if not value.strip():return 'empty'
        if value.isascii() and (value.isdecimal() or re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',value,re.ASCII)):
            return 'invalid_number'
        if len(value)<=65 and re.fullmatch(r'[0-9]{1,32}_[0-9]{1,32}',value,re.ASCII):
            return 'compound_numeric'
        return 'opaque_string'
    return 'invalid_number' if type(value) in (int,float) else 'invalid_type'


def inspect_identity(media):
    media=media if isinstance(media,dict) else {}
    pk=_shape(media,'pk');node=_shape(media,'id')
    present=[shape for shape in (pk,node) if shape!='absent']
    if not present:relation='missing'
    elif any(shape in ('null','empty') for shape in present):relation='incomplete'
    elif any(shape in ('compound_numeric','opaque_string') for shape in present):relation='unsupported_layout'
    elif any(shape!='numeric' for shape in present):relation='invalid'
    elif len(present)==1:relation='single_numeric'
    elif numeric_id(media['pk'])==numeric_id(media['id']):relation='consistent'
    else:relation='conflicting'
    return dict(pk_shape=pk,id_shape=node,relation=relation)


def normalize_identity(media):
    """Return canonical numeric identity or raise a fixed, safe reason.

    pk is the legacy scraper's stored media ID. Decimal id-only legacy payloads
    remain supported. If both numeric fields exist they must agree; any supplied
    unsupported or malformed field blocks, rather than silently falling back.
    """
    details=inspect_identity(media)
    failures=dict(missing='missing_media_identity',incomplete='incomplete_media_identity',
        unsupported_layout='unsupported_media_identity_layout',invalid='invalid_media_identity',
        conflicting='conflicting_media_identity')
    if details['relation'] in failures:raise ValueError(failures[details['relation']])
    return numeric_id(media['pk'] if 'pk' in media else media['id'])
