"""Opt-in, offline-tested evidence adapter. Not connected to production capture.

No live DOM contract is approved yet. Callers must review the actual Posts grid
before supplying a ReviewedGridContract. No default selector, inferred contract,
raw-response fallback, or collaboration acceptance inference is provided.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from urllib.parse import urlsplit

from app.instagram_collaboration import account_name, normalize_collaboration
from app.instagram_scraper.graphql_diagnostics import TIMELINE_ROOTS, classify_error_path, REQUIRED_PATH_FIELDS
from app.instagram_scraper.timeline_media import resolve_timeline_media
from app.instagram_scraper.post_ingestion_policy import (
    ProfileLinkEvidence, ErrorIsolationEvidence, PostObservation, evaluate_posts,
)


@dataclass(frozen=True)
class ReviewedGridContract:
    """Developer-reviewed selectors, never user/CLI-supplied or inferred from links.

    review_reference must identify the fixture/review establishing actual Posts
    grid semantics, exclusion of suggestions/tagged/reposts, and profile header.
    This attestation cannot be verified merely by constructing the Python type.
    Existing repository evidence does not yet supply a real Instagram contract.
    """
    review_reference: str
    profile_header_selector: str
    posts_tab_selector: str
    grid_selector: str
    cell_selector: str


@dataclass(frozen=True)
class NavigationContext:
    profile: str
    document_epoch: float


@dataclass(frozen=True)
class GridMembership:
    """Private in-memory account/code evidence. Do not serialize or log."""
    context: NavigationContext
    observed_at: datetime
    shortcodes: frozenset
    authenticated: bool


def _profile_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in ('www.instagram.com', 'instagram.com') or
                url.username is not None or url.password is not None or url.port not in (None, 443) or
                url.query or url.fragment or '%' in url.path):
            return None
        match = re.fullmatch(r'/([A-Za-z0-9._]{1,30})/?', url.path, re.ASCII)
        profile = account_name(match[1]) if match else None
        return None if profile in ('accounts', 'explore', 'reels', 'direct', 'stories') else profile
    except ValueError:
        return None


def _permalink(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme or url.netloc:
            if (url.scheme != 'https' or url.hostname not in ('www.instagram.com', 'instagram.com') or
                    url.username is not None or url.password is not None or url.port not in (None, 443)):
                return None
        if url.query or url.fragment:
            return None
        match = re.fullmatch(r'/(?:p|reel)/([A-Za-z0-9_-]{1,32})/', url.path, re.ASCII)
        return match[1] if match else None
    except ValueError:
        return None


def _context(value):
    if not isinstance(value, dict):
        return None
    profile = _profile_url(value.get('url')); epoch = value.get('epoch')
    if profile is None or type(epoch) not in (float, int) or not 0 < epoch < 10**16:
        return None
    return NavigationContext(profile, epoch)


def current_navigation(page):
    """Read cached DOM context only; no navigation or network request."""
    try:
        return _context(page.evaluate('() => ({url: location.href, epoch: performance.timeOrigin})'))
    except Exception:
        return None


_READ_GRID = r'''(c) => {
  const one = (root, selector) => {
    const values = root.querySelectorAll(selector);
    return values.length === 1 ? values[0] : null;
  };
  const main = one(document, 'main');
  const header = main && one(main, c.profile_header_selector);
  const tab = main && one(main, c.posts_tab_selector);
  const grid = main && one(main, c.grid_selector);
  const visible = e => !!e && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
  const scoped = !!(main && header && tab && grid && visible(header) && visible(tab) && visible(grid) &&
    tab.getAttribute('role') === 'tab' && tab.getAttribute('aria-selected') === 'true' &&
    tab.id && grid.getAttribute('role') === 'tabpanel' &&
    (grid.getAttribute('aria-labelledby') || '').split(/\s+/).includes(tab.id) &&
    !grid.closest('aside, nav, [role="dialog"]'));
  const cells = scoped ? Array.from(grid.querySelectorAll(c.cell_selector)) : [];
  return {url: location.href, epoch: performance.timeOrigin,
    header: header ? header.textContent.trim() : null,
    tab_href: tab ? tab.getAttribute('href') : null,
    scoped, truncated: cells.length > 1000,
    cells: cells.slice(0, 1000).map(cell => {
      const links = Array.from(cell.querySelectorAll('a[href]'));
      return {scoped: visible(cell) && cell.closest('[role="tabpanel"]') === grid &&
        !cell.closest('aside, nav, [role="dialog"]'), truncated: links.length > 8,
        links: links.slice(0, 8).map(a => visible(a) ? a.getAttribute('href') : null)};
    })};
}'''


def capture_membership(page, requested_profile, *, now, contract=None, authenticated=False):
    """Read only a reviewed scoped grid. Returns (private evidence, safe summary).

    Deliberately not invoked by schema_capture/discovery or any existing CLI.
    No raw DOM values, URLs, accounts or shortcodes enter the summary.
    """
    summary = dict(status='unavailable', reason='reviewed_grid_contract_missing',
                   verified_cells=0, skipped_cells=0, truncated=False)
    if not isinstance(contract, ReviewedGridContract):
        return None, summary
    if (not isinstance(now, datetime) or now.utcoffset() is None or
            not all(isinstance(value, str) and 0 < len(value) <= 512 for value in vars(contract).values())):
        return None, dict(summary, reason='invalid_grid_contract_or_clock')
    profile = account_name(requested_profile)
    if profile is None or authenticated is not True:
        return None, dict(summary, reason='profile_or_authentication_unverified')
    try:
        before = current_navigation(page)
        snapshot = page.evaluate(_READ_GRID, vars(contract))
        after = current_navigation(page)
        context = _context(snapshot)
        if context is None or context != before or context != after or context.profile != profile:
            return None, dict(summary, reason='navigation_context_mismatch')
        tab_href = snapshot.get('tab_href')
        tab_profile = _profile_url('https://www.instagram.com' + tab_href) if isinstance(tab_href, str) and tab_href.startswith('/') else _profile_url(tab_href)
        if (snapshot.get('scoped') is not True or account_name(snapshot.get('header')) != profile or
                tab_profile != profile):
            return None, dict(summary, reason='profile_posts_grid_unverified')
        cells = snapshot.get('cells')
        if not isinstance(cells, list) or len(cells) > 1000:
            return None, dict(summary, reason='malformed_grid_snapshot')
        codes = set()
        for cell in cells:
            if (not isinstance(cell, dict) or cell.get('scoped') is not True or
                    cell.get('truncated') is not False or not isinstance(cell.get('links'), list) or
                    not 1 <= len(cell['links']) <= 8):
                summary['skipped_cells'] += 1; continue
            parsed = [_permalink(link) for link in cell['links']]
            if None in parsed or len(set(parsed)) != 1:
                summary['skipped_cells'] += 1; continue
            codes.add(parsed[0]); summary['verified_cells'] += 1
        summary.update(status='observed', reason='scoped_posts_grid_observed', truncated=snapshot.get('truncated') is True)
        return GridMembership(context, now, frozenset(codes), True), summary
    except Exception:
        return None, dict(summary, reason='grid_read_failed')


def _isolated_errors(errors, root, index, edges, metadata):
    """Narrow disjoint-edge attribution, never 'unknown means optional'.

    Only reviewed consumed-field paths on another concrete edge of this exact
    connection qualify. Unknown fields, ancestors, pagination, absent paths,
    malformed entries, other roots and truncated inspection all remain unresolved.
    """
    if not isinstance(errors, list) or not 1 <= len(errors) <= 128:
        return None
    for error in errors:
        path = error.get('path') if isinstance(error, dict) else None
        detail = classify_error_path(path)
        if (not isinstance(path, list) or len(path) < 5 or path[:2] != [root, 'edges'] or
                type(path[2]) is not int or not 0 <= path[2] < len(edges) or path[2] == index or
                path[3] != 'node' or not isinstance(edges[path[2]], dict) or
                not isinstance(edges[path[2]].get('node'), dict) or
                detail['category'] not in REQUIRED_PATH_FIELDS or detail['contains_unknown_segments'] or
                detail['inspection_truncated']):
            return None
        # Verify that the path's ancestors exist in the actual response, rather
        # than accepting an arbitrary claimed path. A failed leaf may be null or absent.
        value = edges[path[2]]['node']
        for part in path[4:-1]:
            if isinstance(value, dict) and isinstance(part, str) and part in value:
                value = value[part]
            elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
                value = value[part]
            else:
                return None
        leaf = path[-1]
        if not (isinstance(value, dict) and isinstance(leaf, str) or
                isinstance(value, list) and type(leaf) is int and 0 <= leaf < len(value)):
            return None
    return ErrorIsolationEvidence(metadata.post_shortcode, metadata.primary_author, True, True)


def evaluate_response(payload, *, membership, navigation, requested_profile, now,
                      collaboration_evidence=(), existing_posts=(), max_age_days=40):
    """Connect grid evidence and conservative per-edge errors to the offline policy."""
    valid = (isinstance(membership, GridMembership) and isinstance(navigation, NavigationContext) and
             membership.context == navigation and navigation.profile == account_name(requested_profile) and
             isinstance(now, datetime) and now.utcoffset() is not None and
             isinstance(membership.observed_at, datetime) and membership.observed_at.utcoffset() is not None and
             isinstance(membership.shortcodes, frozenset) and
             timedelta(0) <= now - membership.observed_at <= timedelta(minutes=5) and membership.authenticated is True)
    observations = []; reasons = dict(grid_member=0, grid_membership_unverified=0,
                                     collaboration_unconfirmed=0, errors_unresolved=0,
                                     unsupported_connections=0, malformed_connections=0)
    root_data = payload.get('data') if isinstance(payload, dict) else None
    has_errors = not isinstance(payload, dict) or ('errors' in payload and payload['errors'] != [])
    inspected = 0; truncated = False
    if isinstance(root_data, dict):
        truncated = len(root_data) > 64
        for root, connection in list(root_data.items())[:64]:
            if root not in TIMELINE_ROOTS:
                reasons['unsupported_connections'] += 1
                continue
            if not isinstance(connection, dict) or not isinstance(connection.get('edges'), list):
                reasons['malformed_connections'] += 1
                continue
            edges = connection['edges']; truncated |= len(edges) > 128 - inspected
            for index, edge in enumerate(edges[:max(0, 128-inspected)]):
                inspected += 1
                media, _ = resolve_timeline_media(edge)
                metadata = normalize_collaboration(media, requested_profile, accepted_evidence=collaboration_evidence)
                proof = None
                if valid and metadata.post_shortcode in membership.shortcodes and metadata.primary_author:
                    relation = 'author' if metadata.primary_author == navigation.profile else 'confirmed_collaborator'
                    confirmed = any(item.username == navigation.profile and item.status == 'confirmed' for item in metadata.collaborators)
                    if relation == 'author' or confirmed:
                        proof = ProfileLinkEvidence(metadata.post_shortcode, metadata.primary_author,
                            navigation.profile, relation, 'independent_post_verification')
                        reasons['grid_member'] += 1
                    else:
                        reasons['collaboration_unconfirmed'] += 1
                else:
                    reasons['grid_membership_unverified'] += 1
                isolation = _isolated_errors(payload.get('errors'), root, index, edges, metadata) if has_errors else None
                if has_errors and isolation is None:
                    reasons['errors_unresolved'] += 1
                observations.append(PostObservation(edge, requested_profile, bool(valid), has_errors,
                    proof, collaboration_evidence, isolation))
    result = evaluate_posts(observations, now=now, max_age_days=max_age_days, existing_posts=existing_posts)
    result['evidence_adapter'] = dict(mode='offline_grid_evidence', counts=reasons,
                                     inspection_truncated=truncated, membership_current=bool(valid))
    return result
