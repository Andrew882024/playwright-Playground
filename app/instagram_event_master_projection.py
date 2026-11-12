"""Pure Event Master field projection shared with read-only dashboard verification."""
import hashlib
import html
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo


def parent_marker(post_id):
    return f'[eventmaster-import:instagram:playground:instagram_posts:{post_id}]'


def text_field(value, limit, field):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f'Invalid {field}.')
    return value.strip()


def start_time(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('Timezone-aware start required.')
    return value.astimezone(ZoneInfo('America/Los_Angeles')).replace(tzinfo=None)


def field_hash(value):
    if isinstance(value, datetime): value = value.isoformat()
    if isinstance(value, float) and value.is_integer(): value = int(value)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()

def build_event_master_fields(parent, children, build=None):
    if parent['ai_analyzed'] is not True:
        raise ValueError('Parent post analysis must be complete.')
    if not children:
        return None  # Successful zero-event extraction is never a fabricated event.
    if build is None:
        from app.instagram_post_presentation import build_post_presentation
        build = build_post_presentation
    presentation = build(parent, children)
    # Validate each individual interval; never compute a collection-wide duration.
    for child in children:
        duration = child['duration_in_minutes']
        if duration is not None:
            if isinstance(duration, bool) or not math.isfinite(float(duration)) or float(duration) < 0:
                raise ValueError('Activity duration must be finite and nonnegative.')
        start, end = child['event_start_at'], child['event_end_at']
        if start is not None and end is not None and duration is not None:
            from datetime import timezone
            elapsed = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 60
            if not math.isclose(elapsed, float(duration), rel_tol=0, abs_tol=1e-6):
                raise ValueError('Activity end/start and duration are inconsistent.')
    def escape(value):
        return html.escape(str(value or ''), quote=True).replace('\n', '<br />')
    context = presentation['public_context']
    footer = '<br />'.join([
        'Source: Instagram', f"Profile: {escape(parent['profile_username'])}",
        f"Post shortcode: {escape(parent['post_shortcode'])}", f"Post URL: {escape(parent['post_url'])}",
        parent_marker(parent['id']),
    ])
    provider = (parent.get('provider_name') or '').strip() or (parent.get('profile_username') or '').strip()
    unknown_time = presentation['multi_session'] or presentation['has_unknown_start']
    return dict(
        event_title=text_field(presentation['title'], 100, 'aggregate title'),
        event_description=text_field('<p>' + escape(context) + '</p>' + presentation['description'] +
                                     '<p>' + footer + '</p>', 10000, 'aggregate description'),
        event_provider_name=text_field(provider, 100, 'parent provider'),
        showed_event_provider_name=text_field(provider, 100, 'parent provider'),
        event_location=text_field(presentation['location_summary'] or 'Location not provided', 500, 'location'),
        event_start_date_and_time=start_time(presentation['earliest_start']) if presentation['earliest_start'] else None,
        event_duration_in_minutes=presentation['duration_in_minutes'],
        use_undetermined_time=unknown_time,
        undetermined_time_text=presentation['schedule_text'] if unknown_time else '',
        event_imageUrl=text_field(parent.get('generated_image_url') or parent.get('own_s3_url_for_main_image') or
                                 parent.get('main_image_url'), 1000, 'parent image'),
    )
