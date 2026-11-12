"""Allowlisted scrape diagnostics. Never retain URLs, bodies, or exception messages."""
from urllib.parse import urlparse
from app.instagram_scraper.timeline_media import resolve_timeline_media
from app.instagram_scraper.graphql_diagnostics import new_diagnostics

CAPTURE_ERROR_CATEGORIES = (
    'response_body_unavailable', 'json_decoding_failure', 'unexpected_json_structure',
    'missing_timeline_edges', 'unsupported_timeline_schema', 'browser_context_closed',
    'response_processing_exception', 'http_error', 'graphql_error', 'request_failed',
)


def browser_closed(exc):
    # Match known Playwright closure signals privately; never retain exception text.
    from playwright.sync_api import Error as PlaywrightError
    return isinstance(exc, PlaywrightError) and (
        type(exc).__name__ == 'TargetClosedError' or
        'Target page, context or browser has been closed' in str(exc))


def capture_error(health, category):
    if category not in CAPTURE_ERROR_CATEGORIES:
        category = 'response_processing_exception'
    health['capture_errors'] += 1
    health['error_categories'][category] += 1
    if health.get('best_effort'):
        health.setdefault('response_warnings', []).append(dict(level='warning', reason=category))


def page_failure(page):
    parsed = urlparse(page.url)
    path = parsed.path.lower()
    if path.startswith('/accounts/login'):
        return 'login_required'
    if path.startswith(('/challenge', '/checkpoint', '/accounts/suspended', '/accounts/disabled')):
        return 'authentication_challenge'
    if parsed.hostname not in ('www.instagram.com', 'instagram.com'):
        return 'unexpected_navigation'
    # Return booleans only: do not copy page text into diagnostics.
    if page.locator('input[name="username"], input[name="password"]').count():
        return 'login_required'
    if page.locator('form[action*="challenge"], form[action*="checkpoint"]').count():
        return 'authentication_challenge'
    return None


def new_capture_health():
    return dict(graphql_responses=0, valid_timeline_responses=0, capture_errors=0,
                valid_empty_timeline_responses=0, ignored_graphql_responses=0,
                error_categories={key: 0 for key in CAPTURE_ERROR_CATEGORIES}, http_status_codes=[],
                **new_diagnostics())


def classify_timeline(data, nodes, *, post_data_hint=False):
    """Diagnose supported top-level connections without adding extraction schemas."""
    if not isinstance(data, dict) or data.get('errors'):
        return 'unexpected_json_structure'
    root = data.get('data')
    if not isinstance(root, dict):
        return 'unexpected_json_structure'
    connections = [(key, value) for key, value in root.items()
                   if any(marker in key.lower() for marker in ('timeline', 'profile_grid'))]
    # The existing extractor also accepts top-level generic edges containing code nodes.
    if nodes:
        connections += [(key, value) for key, value in root.items()
                        if isinstance(value, dict) and isinstance(value.get('edges'), list)
                        and (key, value) not in connections]
    for _, value in connections:
        if not isinstance(value, dict):
            return 'unexpected_json_structure'
        if 'edges' not in value:
            return 'unsupported_timeline_schema' if any(key in value for key in ('items','nodes')) else 'missing_timeline_edges'
        edges = value['edges']
        if not isinstance(edges, list):
            return 'unexpected_json_structure'
        if any(resolve_timeline_media(edge)[0] is None for edge in edges):
            return 'unsupported_timeline_schema'
    if connections:
        if not nodes and any(value['edges'] for _, value in connections):
            return 'unsupported_timeline_schema'
        return 'valid_timeline' if nodes else 'valid_empty_timeline'
    # Nested/renamed timeline payloads are diagnosed, never silently extracted.
    def nested_timeline(value):
        if isinstance(value, dict):
            return any(any(marker in key.lower() for marker in ('timeline','profile_grid')) or nested_timeline(child)
                       for key, child in value.items())
        if isinstance(value, list):
            return any(nested_timeline(child) for child in value)
        return False
    if nested_timeline(root) or post_data_hint:
        return 'unsupported_timeline_schema'
    if not root:
        return 'missing_timeline_edges'
    return 'ignored_graphql_response'


def valid_timeline(data, nodes):
    return classify_timeline(data, nodes) in ('valid_timeline','valid_empty_timeline')


def capture_failure(health):
    if health.get('best_effort'):
        # Authentication/transport closure remains global, even with valid siblings.
        codes = health.get('graphql_error_diagnostics', {}).get('code_counts', {})
        if (health['error_categories'].get('browser_context_closed')
                or any(code in health.get('http_status_codes', []) for code in (401, 403))
                or any(codes.get(str(code), 0) for code in (102, 190))):
            return 'global_capture_failure'
        if health['valid_timeline_responses']:
            return None
    if health['capture_errors']:
        return 'response_capture_failed'
    if not health['valid_timeline_responses']:
        return 'no_valid_timeline_response'
    return None
