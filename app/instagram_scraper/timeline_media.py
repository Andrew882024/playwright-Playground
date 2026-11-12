"""Narrow, shared timeline resolver. No recursive media discovery or network I/O."""
from datetime import datetime, timezone

RESOLUTION_KINDS = ('flat_media', 'nested_media', 'null_placeholder', 'malformed', 'unsupported')


def _valid_media(media, *, nested):
    code = media.get('code')
    if not isinstance(code, str) or not code.strip():
        return False
    # Legacy flat payloads may omit these fields; validate them whenever supplied.
    for key in ('id', 'pk'):
        if key in media and not (isinstance(media[key], str) and media[key].strip() or
                                 type(media[key]) is int and media[key] > 0):
            return False
    for key in ('taken_at', 'device_timestamp'):
        if key in media:
            value = media[key]
            if type(value) is not int or value <= 0:
                return False
            try:
                datetime.fromtimestamp(value, timezone.utc)
            except (ValueError, OverflowError, OSError):
                return False
    if 'caption' in media and media['caption'] is not None:
        caption = media['caption']
        if not isinstance(caption, dict) or not isinstance(caption.get('text'), str):
            return False
    if 'user' in media and (not isinstance(media['user'], dict) or
                           not isinstance(media['user'].get('username'), str) or
                           not media['user']['username'].strip()):
        return False
    if 'image_versions2' in media:
        images = media['image_versions2']
        if not isinstance(images, dict) or not isinstance(images.get('candidates'), list):
            return False
        if not images['candidates'] or any(not isinstance(item, dict) or
                not isinstance(item.get('url'), str) or not item['url'].strip()
                for item in images['candidates']):
            return False
    if nested:
        return (any(key in media for key in ('id', 'pk')) and 'taken_at' in media and
                all(key in media for key in ('caption', 'user', 'image_versions2')))
    return True


def resolve_timeline_media(edge):
    """Return (media, fixed outcome).

    null_placeholder is a structural candidate, not a verified harmless slot.
    Neither null node nor explicit node.media=null proves that media can be
    omitted safely. Health validation rejects these even beside valid posts.
    """
    if not isinstance(edge, dict) or 'node' not in edge:
        return None, 'malformed'
    node = edge['node']
    if node is None:
        return None, 'null_placeholder'
    if not isinstance(node, dict):
        return None, 'malformed'
    if 'code' in node:
        return (node, 'flat_media') if _valid_media(node, nested=False) else (None, 'malformed')
    if 'media' not in node:
        return None, 'unsupported'
    media = node['media']
    if media is None:
        return None, 'null_placeholder'
    if not isinstance(media, dict) or not _valid_media(media, nested=True):
        return None, 'malformed'
    result = dict(media)
    # Pin information can live on the wrapper; preserve the existing recency safeguard.
    for key in ('is_pinned', 'is_pinned_to_profile'):
        if key in node:
            if type(node[key]) is not bool:
                return None, 'malformed'
            result[key] = bool(result.get(key)) or node[key]
    return result, 'nested_media'
