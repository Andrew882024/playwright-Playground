"""Narrow exceptions evidenced by the sixthcoboard capture; no media discovery.

The home-feed operation contains ads/recommendations and is not profile data.
A location avatar is excluded by sync_ins._cdn_url_key_excluded, unlike post
images, captions, timestamps, comments, pagination or organizer identities.
"""
import json
from urllib.parse import parse_qs

PROFILE_ROOT = 'xdt_api__v1__feed__user_timeline_graphql_connection'
HOME_ROOT = 'xdt_api__v1__feed__timeline__connection'
FIELD_EXCEPTION = 'A server error field_exception occured. Check server logs for details.'


def operation(response):
    """Inspect bounded metadata transiently; never return arbitrary request values."""
    try:
        request = response.request
        body = request.post_data
        if request.method != 'POST' or not isinstance(body, str) or len(body) > 131072:
            return None
        from app.instagram_scraper.query_transport import _json
        if body.lstrip().startswith('{'):
            fields = _json(body)
        else:
            fields = parse_qs(body, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=128, errors='strict')
            if any(len(values) != 1 for values in fields.values()):
                return None
            fields = {key: values[0] for key, values in fields.items()}
        if not isinstance(fields, dict):
            return None
        names = [fields[key] for key in ('fb_api_req_friendly_name', 'operationName') if key in fields]
        if not names or any(name != names[0] for name in names):
            return None
        return names[0] if names[0] in (
            'PolarisFeedTimelineRootV2Query', 'PolarisProfilePostsTabContentQuery_connection') else None
    except Exception:
        return None


def unrelated_home_feed(response, data):
    root = data.get('data')
    return (not data.get('errors') and isinstance(root, dict) and set(root) == {HOME_ROOT}
            and isinstance(root[HOME_ROOT], dict) and isinstance(root[HOME_ROOT].get('edges'), list)
            and operation(response) == 'PolarisFeedTimelineRootV2Query')


def location_avatar_errors_only(response, data):
    """Accept only observed leaf-null failures; caller must still validate ALL edges."""
    if operation(response) != 'PolarisProfilePostsTabContentQuery_connection':
        return False
    root = data.get('data')
    errors = data.get('errors')
    if (not isinstance(root, dict) or PROFILE_ROOT not in root
            or set(root) - {PROFILE_ROOT, 'xdt_viewer'}
            or not isinstance(errors, list) or not 1 <= len(errors) <= 32):
        return False
    connection = root[PROFILE_ROOT]
    if not isinstance(connection, dict) or not isinstance(connection.get('edges'), list):
        return False
    edges = connection['edges']
    for error in errors:
        if (not isinstance(error, dict) or error.get('message') != FIELD_EXCEPTION
                or error.get('severity') != 'ERROR'
                or set(error) - {'message', 'severity', 'path', 'locations'}):
            return False
        path = error.get('path')
        if (not isinstance(path, list) or len(path) != 6
                or path[:2] != [PROFILE_ROOT, 'edges'] or type(path[2]) is not int
                or not 0 <= path[2] < len(edges)
                or path[3:] != ['node', 'location', 'profile_pic_url']):
            return False
        edge = edges[path[2]]
        node = edge.get('node') if isinstance(edge, dict) else None
        location = node.get('location') if isinstance(node, dict) else None
        if (not isinstance(location, dict) or 'profile_pic_url' not in location
                or location['profile_pic_url'] is not None):
            return False
    return True
