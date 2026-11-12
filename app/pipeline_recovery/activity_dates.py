"""Automatic paid-generation eligibility from committed activity snapshots."""
import json
from datetime import datetime, timezone


def aware(value):
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
        return dt.astimezone(timezone.utc) if isinstance(dt,datetime) and dt.utcoffset() is not None else None
    except (ValueError,TypeError,OverflowError):
        return None


def generation_policy(snapshot, now=None):
    try: activities=json.loads(snapshot.context)['activities']
    except (ValueError,TypeError,KeyError):return 'activity_date_review_required'
    if not isinstance(activities,list):return 'activity_date_review_required'
    dates=[]
    for activity in activities:
        if not isinstance(activity,dict):continue
        end=aware(activity.get('end'))
        start=aware(activity.get('start'))
        if end is not None and start is not None and end<start:end=None
        date=end or start
        if date is not None:dates.append(date)
    if not dates:return 'activity_date_review_required'
    return 'skipped_expired' if max(dates)<=(now or datetime.now(timezone.utc)) else None


def require_generation_date(snapshot, reserve=False):
    """Automatic normal/recovery callers only; manual CLI behavior stays unchanged."""
    from app.event_cover_service.openai_cover import CoverError
    reason=generation_policy(snapshot)
    if reason:raise CoverError('Automatic generation blocked by activity date policy.',reason=reason)
