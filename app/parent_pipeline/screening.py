"""Conservative local caption screening. No AI, network, or database operations."""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MONTHS = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
_MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)'
_DATES = re.compile(r'\b('+_MONTH+r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*[-–—]\s*(\d{1,2})(?:st|nd|rd|th)?)?(?:\s*,?\s*((?:19|20|21)\d{2}))?\b', re.I)
_ISO = re.compile(r'\b((?:19|20|21)\d{2})-(\d{2})-(\d{2})\b')
# Slash dates only; colons/digits prevent matching fragments of clock ranges.
_NUMERIC = re.compile(r'(?<![\w:/-])(\d{1,2})/(\d{1,2})(?:/((?:19|20|21)\d{2}))?(?![\w:/-])')
_UNRESOLVED_NUMERIC = re.compile(r'(?<![\w:])\d{1,2}[/-]\d{1,2}(?![\w:])')
_TIME_RANGE = re.compile(r'(?<![\w:/-])(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)?\s*[-–—]\s*(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)?(?![\w:/-])', re.I)
_WEEKDAYS = ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')
_DAY = r'(?:'+'|'.join(_WEEKDAYS)+r')'
_WEEKDAY = re.compile(r'\b('+_DAY+r')s?\b', re.I)
_RELATIVE = re.compile(r'\b(today|tonight|tomorrow|yesterday|(?:this|next|last)\s+(?:'+_DAY+r'|week|weekend|month|year|quarter|semester|season))\b', re.I)
_UNRESOLVED_FUTURE = re.compile(r'\b(?:dates?\s+(?:tbd|tba|to be announced)|(?:another|additional)\s+(?:event|activity|session|workshop|talk)\s+(?:soon|coming))\b', re.I)
_UNCERTAIN_PREFIX = re.compile(r'\b(?:maybe|possibly|tentative(?:ly)?|probably|around|sometime|not)\s+(?:on\s+)?$', re.I)
_NON_EVENT = re.compile(r'\b(apply|applications?|deadline|voucher|redeem|promotion|discount|sale|recap|anniversary|founded|birthday|took place|thank you for (?:coming|joining)|thanks for (?:coming|joining)|throwback|we hosted|we held|highlights from|look back)\b', re.I)
_EVENT = re.compile(r'\b(events?|workshops?|socials?|hangouts?|meetups?|meetings?|conferences?|concerts?|talks?|gbms?|sessions?|classes|class|schools?|parties|party|performances?|join us)\b', re.I)
_RECURRING = re.compile(r'\b(every(?:\s+other)?|each)\s+('+_DAY+r')s?\b', re.I)


def _publication_date(get):
    """Actual publication date in LA; legacy naive timestamps are UTC."""
    posted = get('posted_time')
    if not isinstance(posted, datetime):
        seconds = get('posted_unix_seconds')
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        try:
            posted = datetime.fromtimestamp(seconds, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted.astimezone(ZoneInfo('America/Los_Angeles')).date()


def _relative_dates(phrase, published):
    """Return plausible dates, never roll a date forward relative to execution."""
    phrase = ' '.join(phrase.lower().split())
    offsets = {'today':0, 'tonight':0, 'tomorrow':1, 'yesterday':-1}
    if phrase in offsets:
        return [published + timedelta(days=offsets[phrase])], None
    modifier, weekday = phrase.split()
    if modifier not in ('this', 'next') or weekday not in _WEEKDAYS:
        return [], 'relative_date_requires_review'
    delta = _WEEKDAYS.index(weekday) - published.weekday()
    if modifier == 'this':
        offsets = [delta, delta+7] if delta < 0 else [delta]
    else:
        offsets = [delta, delta+7] if delta > 0 else [delta+7]
    reason = 'ambiguous_'+modifier+'_weekday' if len(offsets) > 1 else None
    return [published + timedelta(days=d) for d in offsets], reason


def screen(parent, now=None):
    get = lambda key: parent.get(key) if isinstance(parent, dict) else getattr(parent, key, None)
    caption, title = get('caption'), get('post_title')
    text = caption if isinstance(caption, str) else ''
    if isinstance(title, str) and title and title not in text:
        text += '\n' + title
    # Mask only the founding clause, never the whole paragraph: another event
    # or deadline elsewhere in it must still be examined.
    text, biography_count = re.subn(r'\bfounded\s+in\s+(?:19|20|21)\d{2}\b', ' ', text, flags=re.I)
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/Los_Angeles')).date()
    published = _publication_date(get)
    year = published.year if published else None
    confirmed, plausible, inferences, uncertainties, issues, spans = set(), set(), [], [], [], []

    def problem(reason):
        if reason not in issues:
            issues.append(reason)

    def result(status, reason):
        return dict(status=status, reason=reason, dates=sorted(d.isoformat() for d in confirmed | plausible),
                    confirmed_dates=sorted(d.isoformat() for d in confirmed),
                    timezone='America/Los_Angeles', method='local_caption_dates_v4',
                    publication_year=year, publication_date=published.isoformat() if published else None,
                    date_inferences=inferences, uncertainties=uncertainties, reasons=list(dict.fromkeys([reason]+issues)),
                    ignored_context=['organization_founding_year'] if biography_count else [])

    def record(parsed, source, phrase=None):
        confirmed.add(parsed)
        item = dict(date=parsed.isoformat(), year_source=source)
        if phrase is not None:
            item['relative_phrase'] = ' '.join(phrase.lower().split())
        if item not in inferences:
            inferences.append(item)

    def check_label(match, parsed):
        offset = max(0, match.start()-20)
        label = re.search(r'\b('+_DAY+r')\s*,?\s*$', text[offset:match.start()], re.I)
        if label:
            if _WEEKDAYS[parsed.weekday()] != label.group(1).lower():
                problem('weekday_date_conflict')
            spans.append((offset+label.start(), offset+label.end()))

    absolute_matches = list(_ISO.finditer(text)) + list(_DATES.finditer(text)) + list(_NUMERIC.finditer(text))
    absolute_by_span = {}
    # 10/11 could mean October 11 OR November 10. Another activity's date
    # must not silently establish its order; 10/14 and 11/11 are unambiguous.
    for pattern in (_ISO, _DATES, _NUMERIC):
        for match in pattern.finditer(text):
            spans.append(match.span())
            try:
                if pattern is _ISO:
                    y, month, day = map(int, match.groups())
                    end, source = None, 'explicit_year'
                else:
                    if pattern is _DATES:
                        month, day, end, y = match.groups()
                        month = _MONTHS[month[:3].lower()]
                    else:
                        month, day, y = match.groups()
                        month, end = int(month), None
                    source = 'explicit_year' if y else 'publication_year'
                    if not y and year is None:
                        problem('event_year_uncertain')
                        continue
                    y, day = int(y or year), int(day)
                parsed = date(y, month, day)
                check_label(match, parsed)
                if pattern is _NUMERIC and month != day and day <= 12:
                    choices = sorted({parsed, date(y, day, month)})
                    plausible.update(choices)
                    uncertainties.append(dict(reason='numeric_date_order_uncertain', candidate_dates=[d.isoformat() for d in choices]))
                    inferences.append(dict(candidate_dates=[d.isoformat() for d in choices], year_source=source, certainty='ambiguous'))
                    problem('numeric_date_order_uncertain')
                else:
                    record(parsed, source)
                    absolute_by_span[match.span()] = {parsed}
                if end:
                    if int(end) < day:
                        problem('date_range_uncertain')
                    else:
                        final = date(y, month, int(end))
                        record(final, source)
                        absolute_by_span.setdefault(match.span(), set()).add(final)
            except (ValueError, OverflowError):
                problem('invalid_event_date')

    absolute_dates = set(confirmed)
    relative_matches = list(_RELATIVE.finditer(text))
    for match in relative_matches:
        spans.append(match.span())
        if published is None:
            problem('relative_publication_date_missing')
            continue
        try:
            choices, reason = _relative_dates(match.group(), published)
        except (ValueError, OverflowError):
            problem('invalid_event_date')
            continue
        if not choices:
            problem(reason)
            continue
        # Only an adjacent, agreeing absolute date may disambiguate a relative
        # expression; an unrelated activity's date cannot resolve it.
        adjacent = [m for m in absolute_matches
                    if (0 <= m.start()-match.end() <= 4 and not text[match.end():m.start()].strip(' ,('))
                    or (0 <= match.start()-m.end() <= 4 and not text[m.end():match.start()].strip(' ,('))]
        adjacent_dates = set().union(*(absolute_by_span.get(m.span(), set()) for m in adjacent))
        matching = adjacent_dates.intersection(choices)
        if adjacent and matching:
            choices, reason = sorted(matching), None
        if len(choices) == 1:
            record(choices[0], 'publication_relative_date', match.group())
        else:
            plausible.update(choices)
            item = dict(reason=reason, relative_phrase=' '.join(match.group().lower().split()),
                        candidate_dates=[d.isoformat() for d in choices])
            uncertainties.append(item)
            inferences.append(dict(item, year_source='publication_relative_date', certainty='ambiguous'))
        if absolute_dates and any(d not in absolute_dates for d in choices):
            problem('relative_absolute_date_conflict')

    # Uncertainty applies to date syntax, not arbitrary words elsewhere in a post.
    tokens = sorted({m.span() for p in (_ISO, _DATES, _NUMERIC, _RELATIVE, _WEEKDAY) for m in p.finditer(text)})
    merged = []
    for start, end in tokens:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in merged:
        if _UNCERTAIN_PREFIX.search(text[max(0, start-40):start]):
            problem('ambiguous_relative_context')
    for (_, end), (start, _) in zip(merged, merged[1:]):
        if re.fullmatch(r'\s*(?:or|and/or)\s*', text[end:start], re.I):
            problem('ambiguous_relative_context')
    if re.search(r'\b(cancelled|canceled|postponed|rescheduled)\b', text, re.I):
        problem('ambiguous_relative_context')
    if _UNRESOLVED_FUTURE.search(text):
        problem('unresolved_future_activity')

    remaining = list(text)
    for match in _TIME_RANGE.finditer(text):
        if ':' in match.group() or re.search(r'\b(?:am|pm)\b|\d(?:am|pm)\b', match.group(), re.I):
            spans.append(match.span())
    for start, end in spans:
        remaining[start:end] = ' '*(end-start)
    remaining = ''.join(remaining)
    bare_weekdays = list(_WEEKDAY.finditer(remaining))
    if bare_weekdays:
        recurring = list(_RECURRING.finditer(text))
        labels = {m.group(1).lower() for m in bare_weekdays}
        if len(labels) == 1 and recurring and absolute_dates and not plausible:
            target = _WEEKDAYS.index(next(iter(labels)))
            if any(d.weekday() != target for d in absolute_dates):
                problem('weekday_date_conflict')
            if any('other' in m[1].lower() for m in recurring):
                ordered = sorted(absolute_dates)
                if any((d-ordered[0]).days % 14 for d in ordered):
                    problem('recurring_cadence_conflict')
        else:
            problem('unanchored_weekday')
    if _UNRESOLVED_NUMERIC.search(remaining) or re.search(r'\b'+_MONTH+r'\b', remaining, re.I):
        problem('unresolved_or_ambiguous_dates')

    announcement = (re.search(r'\b(lineup|come dance|doors open|performing live)\b', text, re.I)
                    and confirmed and re.search(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b', text, re.I)
                    and re.search(r'\b(catch|come dance|doors open|performing live|live sets?)\b', text, re.I)
                    and re.search(r'📍|\b(?:at the|venue|lawn|the loft)\b', text, re.I))
    if not text.strip():
        return result('review_required', 'missing_caption_dates')
    if _NON_EVENT.search(text):
        return result('review_required', 'non_event_or_mixed_date_context')
    if not (_EVENT.search(text) or announcement):
        return result('review_required', 'event_intent_uncertain')
    if issues:
        return result('review_required', issues[0])
    all_dates = confirmed | plausible
    if not all_dates:
        return result('review_required', 'missing_event_dates')
    if max(all_dates) < today:
        if uncertainties:
            return result('skipped_expired', 'all_plausible_event_dates_expired')
        inferred = any(i['year_source'] != 'explicit_year' for i in inferences)
        return result('skipped_expired', 'inferred_event_dates_expired' if inferred else 'explicit_event_dates_expired')
    if uncertainties:
        return result('review_required', uncertainties[0]['reason'])
    if max(all_dates) == today:
        return result('review_required', 'event_today_time_uncertain')
    inferred = any(i['year_source'] != 'explicit_year' for i in inferences)
    return result('eligible', 'inferred_future_event_date' if inferred else 'explicit_future_event_date')


def eligible(parent,override_ids=(),now=None):
    get=lambda key:parent.get(key) if isinstance(parent,dict) else getattr(parent,key,None)
    from app.ai_analyze_service.gemini_analysis import _image_urls_from_post
    from app.parent_pipeline.attempt_safety import review_required
    if review_required(get('id')):return dict(status='review_required',reason='previous_analysis_outcome_requires_review')
    media=get('additional_image_urls')
    if not _image_urls_from_post(dict(openable_media_urls=media if isinstance(media,dict) else {})):
        return dict(status='review_required',reason='missing_usable_image_references')
    result=screen(parent,now)
    if result['status']=='review_required' and get('id') in override_ids:
        return dict(result,status='eligible',reason='explicit_manual_review_override',original_reason=result['reason'],manual_override=True)
    return result
