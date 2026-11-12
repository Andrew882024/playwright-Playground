"""Shared Gemini evidence contract and pure classification, not a second analyzer.

Statuses describe source-supported model output, not verified Instagram origin.
No OCR, requests, database access, or production eligibility decisions occur here.
"""
from datetime import datetime

EVIDENCE_INSTRUCTION='''Event evidence policy:
- The first original post image is the primary visual source; the Instagram caption is complementary text evidence.
- Combine consistent evidence across the first image and caption for event name, date/time and location.
  Do not require all three fields to appear in both sources, or duplicate an activity mentioned in both.
- Read the caption itself; thread replies and a derived title are not authoritative replacements for it.
- Later carousel images are supplemental; preserve every independently attendable activity in events[].
- Never label an attachment as the first original image unless the supplied image-order evidence verifies it.
- For each event include field_sources: keys event_title, start, location; values are lists containing
  only first_image, caption, supplemental_image. Include only sources actually supporting each field.
- Include conflicting_fields (event_title, start, location). If image/caption claims materially disagree
  for the SAME activity, flag that field and use null for disputed time/location; do not silently choose
  one source or invent an extra activity to hide a conflict. Explain concerns in private raw_notes only.
- Missing information is not a contradiction. Keep a recognizable potential event with unknown fields null.
- confirmed_event requires supported event name, valid date/time and location without conflicts;
  potential_event has missing details/evidence; conflicting_information has conflicting claims;
  events=[] means non_event only when no attendable activity is supported, not a failed image download.
- Image-only/media completeness and source verification are separate upstream requirements.
  An image URL, declared media_type=1, or this evidence classification never proves those requirements.
'''


def classify_event(event):
    get=lambda key:getattr(event,key,None)
    if get('conflicting_fields'):return 'conflicting_information'
    start=get('start')
    try:
        values=[getattr(start,key,None) for key in ('year','month','day','hour','minute')]
        complete=all(type(value) is int for value in values)
        if complete:datetime(*values)
    except (ValueError,TypeError):complete=False
    sources=get('field_sources') or {}
    evidence=all(sources.get(key) for key in ('event_title','start','location'))
    title=get('event_title');location=get('location')
    return ('confirmed_event' if complete and isinstance(title,str) and title.strip() and
            isinstance(location,str) and location.strip() and evidence else 'potential_event')


def classify_post(events):
    statuses=[classify_event(event) for event in events]
    if not statuses:return 'non_event'
    if 'conflicting_information' in statuses:return 'conflicting_information'
    return 'potential_event' if 'potential_event' in statuses else 'confirmed_event'


def prioritize_images(urls,post):
    """Only a trusted ordered-media adapter may supply image_order_verified=True.

    Legacy largest lists were sorted by CDN asset, not original carousel order.
    Never infer an original first slide from that order or an AI-selected cover.
    """
    first=post.get('first_image_url')
    verified=post.get('image_order_verified') is True and isinstance(first,str) and first in urls
    return ([first]+[url for url in urls if url!=first] if verified else list(urls))


def image_evidence_note(post,loaded):
    first=post.get('first_image_url')
    verified=post.get('image_order_verified') is True and isinstance(first,str)
    if verified and first in loaded:
        return 'Original first-image order verified by input adapter; attachment 1 is the primary visual source.'
    if verified:
        return 'The original first image was not loaded. Do not claim first_image evidence; use caption and supplemental images only.'
    return ('Original first-image order is unverified for this legacy input. Attachment order is not proof of carousel order; '
            'do not claim first_image evidence. Use caption and loaded images as supplemental evidence.')


def constrain_sources(analysis,post,loaded,caption_present):
    """Discard impossible model source claims without another generation request.

    This checks attachment/text availability only; it does not authenticate a
    source or verify OCR. Structured event title/time/location remain untouched.
    """
    first=post.get('first_image_url')
    verified=post.get('image_order_verified') is True and isinstance(first,str) and first in loaded
    available=set()
    if caption_present:available.add('caption')
    if verified:available.add('first_image')
    if any(not verified or url!=first for url in loaded):available.add('supplemental_image')
    events=[]
    for event in analysis.events:
        fields={key:list(dict.fromkeys(source for source in sources if source in available))
                for key,sources in event.field_sources.items()}
        events.append(event.model_copy(update={'field_sources':fields}))
    return analysis.model_copy(update={'events':events})
