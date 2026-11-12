"""Pure deterministic parent presentation. No database, network, or AI calls."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DISPLAY_ZONE = ZoneInfo('America/Los_Angeles')
VERSION = 'instagram-post-presentation-v1'
# Matches Event Master's Events.event_title = String(100).
EVENT_TITLE_MAX_LENGTH = 100
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def _short_title(value, limit=EVENT_TITLE_MAX_LENGTH):
    value = _text(value)
    if (not value or len(value) > limit or '\n' in value or '\r' in value
            or re.search(r'https?://|<[^>]+>', value, re.IGNORECASE)):
        return None
    return value


def _date_range(dates):
    # Fixed English month names avoid process-locale dependent fingerprints.
    if not dates:
        return ''
    first, last = dates[0], dates[-1]
    if first == last:
        return first.isoformat()
    a, b = MONTHS[first.month - 1], MONTHS[last.month - 1]
    if first.year == last.year and first.month == last.month:
        return f'{a} {first.day}–{last.day}, {first.year}'
    if first.year == last.year:
        return f'{a} {first.day}–{b} {last.day}, {first.year}'
    return f'{a} {first.day}, {first.year}–{b} {last.day}, {last.year}'


def _aggregate_title(parent, activities, providers, dates):
    if len(activities) == 1:
        explicit = _short_title(activities[0]['title'])
    else:
        explicit = _short_title(_get(parent, 'collection_title'))
    if explicit:
        return explicit
    date_text = _date_range(dates)
    suffix = ' — ' + date_text if date_text else ''
    budget = EVENT_TITLE_MAX_LENGTH - len(suffix)
    # Select a complete short identity; never slice a caption, title or provider.
    candidates = [_get(parent, 'provider_name')]
    if len(providers) == 1:
        candidates.extend(providers)
    candidates.extend([_get(parent, 'profile_username'), 'Activities'])
    identity = next(title for value in candidates if (title := _short_title(value, budget)))
    return identity + suffix



def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _text(value):
    return value.strip() if isinstance(value, str) else ''


# Display-only fail-closed filtering, never a rewrite of stored extraction data.
# Drop the whole prose field on a recognizable editorial warning, rather than
# leaving a fragment that could change the warning's meaning.
_EDITORIAL = re.compile(
    r"date discrepancy|potential.{0,30}discrepancy|please note|fact[- ]?check|"
    r"(?:verify|confirm|check).{0,80}(?:organizer|date|schedule|detail)|"
    r"(?:uncertain|ambiguity|ambiguous|inconsisten|discrepanc)|"
    r"(?:as an ai|ai reasoning|cannot (?:verify|confirm)|may be incorrect)|"
    r"(?:falls? on|day of the week|weekday).{0,80}(?:202[0-9]|disagree|rather|however)|"
    r"(?:建议|请).{0,20}(?:核实|确认)|日期.{0,15}(?:不符|矛盾)",
    re.IGNORECASE,
)


def public_description(value, max_words=60):
    """Select up to two complete source sentences; never generate new prose.

    This conservative guard catches known editorial patterns, not all possible
    hallucinations. Suspicious fields are omitted; structured facts remain.
    """
    value = _text(value)
    if not value or _EDITORIAL.search(value):
        return ''
    sentences = re.split(r'(?<=[.!?。！？])\s+|[\r\n]+', value)
    selected = []
    words = 0
    for sentence in sentences:
        sentence = sentence.strip()
        count = len(sentence.split())
        if not sentence or words + count > max_words or len(sentence) > 600:
            break
        selected.append(sentence)
        words += count
        if len(selected) == 2:
            break
    return ' '.join(selected)


def _instant(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('Activity timestamps must be timezone-aware or null.')
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None


def _display(value):
    return value.astimezone(DISPLAY_ZONE).strftime('%Y-%m-%d %H:%M %Z') if value else 'Time not provided'


def build_post_presentation(parent, children):
    """Preserve every supplied child, sorted by known start then title/id.

    earliest_start is the earliest *known* instant, never a fabricated schedule.
    Aggregate duration is null for multiple activities, including adjacent ones.
    Child cover URLs are deliberately excluded from presentation identity.
    """
    activities = []
    for child in children:
        if _get(child, 'instagram_post_id') != _get(parent, 'id'):
            raise ValueError('Activity belongs to a different parent.')
        start, end = (_instant(_get(child, name)) for name in ('event_start_at', 'event_end_at'))
        if start and end and end < start:
            raise ValueError('Activity end precedes start.')
        activities.append(dict(
            id=_get(child, 'id'), title=_text(_get(child, 'event_title')),
            provider=_text(_get(child, 'provider_name')),
            description=_text(_get(child, 'event_description')),
            location=_text(_get(child, 'location')), start=start, end=end,
            duration_in_minutes=_get(child, 'duration_in_minutes'),
        ))
    activities.sort(key=lambda a: (a['start'] is None,
                    a['start'] or datetime.max.replace(tzinfo=timezone.utc), a['title'], a['id']))
    providers = sorted({a['provider'] for a in activities if a['provider']})
    provider = ' / '.join(providers) or _text(_get(parent, 'provider_name')) or None
    dates = sorted({a['start'].astimezone(DISPLAY_ZONE).date()
                    for a in activities if a['start']})
    title = _aggregate_title(parent, activities, providers, dates)
    locations = sorted({a['location'] for a in activities if a['location']})
    unknown_locations = any(not a['location'] for a in activities)
    location_summary = ('Multiple Locations' if len(locations) > 1 else
                        locations[0] if locations and not unknown_locations else None)
    starts = [a['start'] for a in activities if a['start']]
    esc = lambda s: html.escape(s, quote=True).replace('\n', '<br />')
    lines = []
    for activity in activities:
        activity['display_start'] = _display(activity['start'])
        activity['display_end'] = _display(activity['end'])
        lines.append('<li><strong>' + esc(activity['title'] or 'Activity title not provided') +
                     '</strong><br />Start: ' + esc(activity['display_start']) +
                     '<br />End: ' + esc(activity['display_end']) +
                     '<br />Location: ' + esc(activity['location'] or 'Location not provided') +
                     ('<br />' + esc(public_description(activity['description'])) if public_description(activity['description']) else '') + '</li>')
    multi = len(activities) > 1
    schedule_text = 'Multiple activities; see individual schedules.' if multi else (
        activities[0]['display_start'] if activities else 'No activities extracted.')
    input_data = dict(version=VERSION, parent_id=_get(parent, 'id'), title=title,
                     provider=provider, display_timezone=DISPLAY_ZONE.key,
                     source=dict(profile=_text(_get(parent, 'profile_username')),
                                 shortcode=_text(_get(parent, 'post_shortcode'))),
                     activities=[{**a, 'start': _iso(a['start']), 'end': _iso(a['end'])} for a in activities])
    fingerprint = hashlib.sha256(json.dumps(input_data, sort_keys=True, ensure_ascii=False,
                                           separators=(',', ':')).encode()).hexdigest()
    return dict(title=title, provider=provider, activities=activities,
                earliest_start=min(starts) if starts else None,
                has_unknown_start=any(a['start'] is None for a in activities),
                location_summary=location_summary, locations=locations,
                has_unknown_location=unknown_locations, multi_session=multi,
                schedule_text=schedule_text,
                duration_in_minutes=activities[0]['duration_in_minutes'] if len(activities) == 1 else None,
                public_context=public_description(_get(parent, 'post_description') or _get(parent, 'caption'), max_words=100),
                description='<p><strong>Activities</strong></p><ul>' + ''.join(lines) + '</ul>',
                input_data=input_data, fingerprint=fingerprint)


def canonical_cover_inputs(parent, children):
    """Actual aggregate cover context, independent of child IDs/input ordering."""
    data = build_post_presentation(parent, children)['input_data']
    data['activities'] = [{k: v for k, v in a.items() if k != 'id'} for a in data['activities']]
    data['activities'].sort(key=lambda a: json.dumps(a, sort_keys=True, ensure_ascii=False))
    return data


def import_source_revision(parent, children):
    """Hash of the exact source snapshot consumed by the aggregate importer."""
    data = dict(presentation=canonical_cover_inputs(parent, children), parent={
        k: _get(parent, k) for k in ('id', 'ai_analyzed', 'post_url', 'post_description',
            'caption', 'generated_image_url', 'own_s3_url_for_main_image', 'main_image_url')})
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()
