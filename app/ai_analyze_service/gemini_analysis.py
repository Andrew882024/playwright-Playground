"""Gemini client: schemas, prompts, image fetch, and per-post analysis."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from typing import Any, Literal, Optional
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.ai_analyze_service import diagnostics as diagnostic
from app.ai_analyze_service import http_budget
from app.parent_pipeline import attempt_safety
from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError
from app.ai_analyze_service.event_evidence import (EVIDENCE_INSTRUCTION, classify_event, classify_post, prioritize_images, image_evidence_note, constrain_sources)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

DEFAULT_MODEL = "gemini-2.5-flash"
# DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

# Raised when every model in the chain hits rate/quota or free-tier exhaustion.
ALL_MODELS_EXHAUSTED_MSG = "We ran out of all models."
DEFAULT_TZ = "America/Los_Angeles"
MAX_IMAGES = 6
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_POST_DESC_MIN_WORDS = 50
_POST_DESC_MAX_WORDS = 200

_SYSTEM_INSTRUCTION = """You analyze Instagram posts for a campus/student-audience feed.
Return a single JSON object matching the user's schema. No markdown, no code fences.

Event vs not:
- An EVENT is a scheduled happening with a specific date/time window people can attend
  (workshops, fairs, meetings, performances, deadlines for in-person activities, etc.).
- NOT an event: general announcements, tips, memes, ticket-sale windows without the main event
  date in the post, reposts that are purely promotional text, or vague content with no
  schedulable occurrence.

Rules:
- Return a REQUIRED events array: one item per independently attendable event/session.
- Do NOT merge separate sessions into one first-start-to-last-end time window.
- Duplicate mentions/slides of the same session must not become duplicate events.
- Each event has its own title, host, description, location, start, end, duration, confidence.
- Use the first original image as primary visual evidence and the caption as complementary text; later images are supplemental.
- Resolve relative dates ("this Saturday", "tomorrow") using the reference datetime provided.
- If you cannot resolve year/month/day or time from consistent image/caption evidence, use null for those fields and
  explain in raw_notes. Do not invent a year.
- For events, provider_name is the organizing host (student council, department, etc.); use
  from_username only if no clearer host is named.
- duration should be human-readable when possible (e.g. "7:00 PM – 9:00 PM"). Include
  duration_minutes when you can compute it.
- Times: use 24-hour fields in start/end (hour 0-23, minute 0-59). If only 12h text is given,
  convert and note ambiguity in raw_notes.
- Public description and event_description must contain event facts only, grounded in the source.
  Use 1–2 concise sentences (at most 60 words); return an empty string if no supported descriptive facts exist.
  Never add date discrepancy warnings, fact-check commentary, uncertainty commentary,
  advice to verify with organizers, AI reasoning, or invented information.
  Put any verification concerns, missing information or uncertainty in raw_notes only, never public descriptions.
  events=[] is successful zero-event output; missing events is invalid.
- location: when the post states where something happens (room, building, campus, address,
  city/neighborhood, or "Online" / a meeting link), output a short human-readable string; use null
  if no place is given or it does not apply.
- main_image_url (when images are provided): pick the single image that best represents this post
  in a small feed card. For events, prefer the slide/flyer with the clearest title, date, time, and
  location; avoid a purely decorative slide if another slide has the schedule. For non-events,
  prefer the strongest visual summary (hero graphic, infographic, or clearest message). Prefer
  legible text and the slide that would make someone understand the post without reading the caption.
  The value must be exactly one of the listed loaded image URL strings when multiple URLs exist.
"""

_SYSTEM_INSTRUCTION += "\n" + EVIDENCE_INSTRUCTION


class DateTimeParts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    year: Optional[int] = Field(default=None, ge=1, le=9999)
    month: Optional[int] = Field(default=None, ge=1, le=12)
    day: Optional[int] = Field(default=None, ge=1, le=31)
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    minute: Optional[int] = Field(default=None, ge=0, le=59)
    timezone: Optional[str] = None
    timezone_iana: Optional[str] = None
    assumptions_note: Optional[str] = None


def _word_count(text: str) -> int:
    """Count whitespace-delimited tokens (Gemini word targets)."""
    return len(re.findall(r"\S+", (text or "").strip()))


def _truncate_to_max_words(text: str, max_words: int) -> str:
    words = re.findall(r"\S+", (text or "").strip())
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def _zone_for_datetime_parts(parts: DateTimeParts, default_tz: ZoneInfo) -> ZoneInfo:
    iana = (parts.timezone_iana or "").strip()
    if iana:
        try:
            return ZoneInfo(iana)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown event timezone: {iana}") from exc
    tz_hint = (parts.timezone or "").strip()
    if tz_hint and "/" in tz_hint:
        try:
            return ZoneInfo(tz_hint)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown event timezone: {tz_hint}") from exc
    return default_tz


def _datetime_from_parts(
    parts: DateTimeParts | None,
    default_tz: ZoneInfo,
) -> datetime | None:
    """Require a complete date and clock time; never manufacture midnight."""
    if parts is None:
        return None
    values = (parts.year, parts.month, parts.day, parts.hour, parts.minute)
    if any(value is None for value in values):
        return None
    return datetime(*values, tzinfo=_zone_for_datetime_parts(parts, default_tz))


class ExtractedEventAI(BaseModel):
    """One independently attendable event/session; never an aggregate time window."""

    model_config = ConfigDict(extra="ignore")

    event_title: str
    provider_name: Optional[str]
    event_description: str
    location: Optional[str]
    start: Optional[DateTimeParts]
    end: Optional[DateTimeParts]
    duration_minutes: Optional[int] = Field(ge=0)
    confidence: Optional[Literal["low", "medium", "high"]]
    duration: Optional[str] = None
    raw_notes: Optional[str] = None

    field_sources: dict[Literal["event_title", "start", "location"], list[Literal["first_image", "caption", "supplemental_image"]]] = Field(default_factory=dict)
    conflicting_fields: list[Literal["event_title", "start", "location"]] = Field(default_factory=list)

    @computed_field
    @property
    def detection_status(self) -> str:
        return classify_event(self)

    @model_validator(mode="after")
    def _validate_event(self) -> ExtractedEventAI:
        self.event_title = self.event_title.strip()
        if not self.event_title:
            raise ValueError("event_title must not be blank")
        wc = _word_count(self.event_description)
        if wc > _POST_DESC_MAX_WORDS:
            raise ValueError("event_description must be at most 200 words (legacy-compatible)")
        return self


class PostAnalysisAI(BaseModel):
    """A required events array distinguishes zero events from invalid/failed output."""

    model_config = ConfigDict(extra="ignore")

    events: list[ExtractedEventAI]
    description: str
    raw_notes: Optional[str] = None
    main_image_url: Optional[str] = None
    gemini_model: Optional[str] = None

    @computed_field
    @property
    def detection_status(self) -> str:
        return classify_post(self.events)

    @computed_field
    @property
    def is_event(self) -> bool:
        return bool(self.events)

    @model_validator(mode="after")
    def _validate_description(self) -> PostAnalysisAI:
        if _word_count(self.description) > _POST_DESC_MAX_WORDS:
            raise ValueError("description must be at most 200 words (legacy-compatible)")
        return self


def event_to_db_values(event: ExtractedEventAI, tz: ZoneInfo) -> dict[str, Any]:
    """Convert each session independently; unknown clock times remain null."""
    start = _datetime_from_parts(event.start, tz)
    end = _datetime_from_parts(event.end, tz)
    duration = event.duration_minutes
    if start is not None and end is not None:
        seconds = (end.astimezone(ZoneInfo("UTC")) - start.astimezone(ZoneInfo("UTC"))).total_seconds()
        if seconds <= 0:
            raise ValueError("event end must be after its start")
        actual_minutes = int(seconds / 60)
        if duration is not None and duration != actual_minutes:
            raise ValueError("duration_minutes disagrees with event start/end")
        duration = actual_minutes
    return {
        "event_title": event.event_title,
        "provider_name": (event.provider_name or "").strip() or None,
        "event_description": event.event_description.strip(),
        "location": (event.location or "").strip() or None,
        "event_start_at": start,
        "event_end_at": end,
        "duration_in_minutes": duration,
        "confidence": event.confidence,
    }


def ai_to_child_event_values(ai: PostAnalysisAI, tz: ZoneInfo) -> list[dict[str, Any]]:
    return [event_to_db_values(event, tz) for event in ai.events]


def primary_image_url_from_post(post: dict[str, Any]) -> str | None:
    """First CDN image URL from the scrape bundle (same filters as loads sent to Gemini)."""
    urls = _image_urls_from_post(post)
    return urls[0] if urls else None


def ai_to_db_update_values(
    ai: PostAnalysisAI,
    default_tz: ZoneInfo,
    *,
    fallback_main_image_url: str | None = None,
) -> dict[str, Any]:
    """Project one deterministic child for legacy readers, never an aggregate window."""
    children = ai_to_child_event_values(ai, default_tz)
    def sort_key(event: dict[str, Any]) -> tuple:
        start = event["event_start_at"]
        return (
            start is None,
            start.astimezone(ZoneInfo("UTC")).isoformat() if start else "",
            " ".join(event["event_title"].casefold().split()),
            json.dumps(event, sort_keys=True, default=str),
        )
    representative = min(children, key=sort_key) if children else {}
    chosen = (ai.main_image_url or "").strip()
    fallback = (fallback_main_image_url or "").strip()
    return {
        "is_event": bool(children),
        "event_title": representative.get("event_title"),
        "provider_name": representative.get("provider_name"),
        "post_description": representative.get("event_description", ai.description.strip()),
        "location": representative.get("location"),
        "duration_in_minutes": representative.get("duration_in_minutes"),
        "confidence": representative.get("confidence"),
        "ai_model": ai.gemini_model,
        "ai_analyzed": True,
        "event_start_at": representative.get("event_start_at"),
        "event_end_at": representative.get("event_end_at"),
        "main_image_url": chosen or fallback or None,
    }


def gemini_api_key() -> str:
    import os

    k = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not k:
        print(
            "Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment (.env).",
            file=sys.stderr,
        )
        sys.exit(1)
    return k


def _gemini_model() -> str:
    import os

    return (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()


def gemini_model_chain() -> list[str]:
    """Primary model first, then fallbacks (deduped). After each model exhausts 429 retries, we try the next."""
    import os

    primary = _gemini_model()
    extra = (os.environ.get("GEMINI_MODEL_FALLBACK") or "").strip()
    if extra:
        rest = [p.strip() for p in extra.split(",") if p.strip()]
    else:
        rest = list(DEFAULT_MODEL_FALLBACK_CHAIN)
    seen: set[str] = {primary}
    out = [primary]
    for m in rest:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def default_tz() -> ZoneInfo:
    import os

    name = (os.environ.get("GEMINI_DEFAULT_TZ") or DEFAULT_TZ).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        print(f"Invalid GEMINI_DEFAULT_TZ={name!r}, using {DEFAULT_TZ}.", file=sys.stderr)
        return ZoneInfo(DEFAULT_TZ)


def _looks_like_image_url(url: str) -> bool:
    u = url.lower().split("?", 1)[0]
    if u.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return True
    if "/v/t51." in url and (".jpg" in u or "jpg" in url):
        return True
    return False


def _is_probably_video(url: str) -> bool:
    u = url.lower()
    return ".mp4" in u or "/v/t16/" in u or "video" in u or u.endswith(".mp4")


def _image_urls_from_post(post: dict[str, Any], limit: int = MAX_IMAGES) -> list[str]:
    omu = post.get("openable_media_urls")
    urls: list[str] = []
    if isinstance(omu, dict):
        largest = omu.get("largest")
        if isinstance(largest, list):
            urls.extend(str(u) for u in largest if isinstance(u, str))
    elif isinstance(omu, list):
        urls.extend(str(u) for u in omu if isinstance(u, str))
    out: list[str] = []
    for u in prioritize_images(urls, post):
        if _is_probably_video(u):
            continue
        if not _looks_like_image_url(u):
            continue
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return prioritize_images(out, post)


def _mime_from_url(url: str) -> str:
    base = url.lower().split("?", 1)[0]
    if base.endswith(".png"):
        return "image/png"
    if base.endswith(".webp"):
        return "image/webp"
    if base.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _fetch_image(url: str) -> tuple[bytes, str] | None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
            ctype = resp.headers.get_content_type()
            if ctype and ctype.startswith("image/"):
                mime = ctype
            else:
                mime = _mime_from_url(url)
            if len(data) > 18 * 1024 * 1024:
                return None
            return data, mime
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        diagnostic.event('image_error',exc)
        return None


def _taken_at_reference_line(taken_at: Any, tz: ZoneInfo) -> str:
    if taken_at is None:
        return "Post taken_at: (missing) — use caption only for dates."
    try:
        ts = int(taken_at)
    except (TypeError, ValueError):
        return f"Post taken_at: (invalid {taken_at!r}) — use caption only for dates."
    dt = datetime.fromtimestamp(ts, tz=tz)
    utc = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))
    return (
        f"Post taken_at (Unix seconds): {ts}\n"
        f"Same instant in {tz.key}: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Same instant in UTC: {utc.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        "Use this to resolve relative phrases like 'this Saturday' or 'tomorrow'."
    )


def _caption_text_from_post(post: dict[str, Any]) -> str:
    explicit = post.get("caption")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    raw = post.get("comments")
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict) and c.get("kind") == "caption":
                return (c.get("text") or "").strip()
    leg = post.get("caption") or post.get("comment")
    return (leg.strip() if isinstance(leg, str) else "") or ""


def _user_prompt(
    post: dict[str, Any],
    ref_line: str,
    num_images: int,
    loaded_image_urls: list[str],
) -> str:
    title = post.get("title") or ""
    caption = _caption_text_from_post(post)
    user = post.get("from_username") or ""
    link = post.get("permalink") or ""
    shortcode = post.get("shortcode") or ""
    comments_block = ""
    raw_comments = post.get("comments")
    if isinstance(raw_comments, list) and raw_comments:
        lines: list[str] = []
        n = 0
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            kind = c.get("kind")
            if kind == "caption":
                who = (c.get("username") or user or "?").strip()
                body = (c.get("text") or "").strip()
                n += 1
                lines.append(f"{n}. [caption] @{who}: {body}")
            elif kind == "reply" or kind is None:
                who = (c.get("username") or "?").strip()
                body = (c.get("text") or "").strip()
                n += 1
                lines.append(f"{n}. [reply] @{who}: {body}")
        if lines:
            comments_block = "comments:\n" + "\n".join(lines) + "\n\n"
    text_block = (
        comments_block
        if comments_block
        else f"comments:\n1. [caption] @{user or '?'}: {caption}\n\n"
        if caption
        else ""
    )
    img_note = (
        f"{num_images} image(s) are attached after this text (flyer/carousel)."
        if num_images
        else "No images could be loaded; use the Instagram caption only as source evidence and do not claim visual evidence."
    )
    schema = f"""
Respond with JSON only:
- events (REQUIRED array): [] for a successful analysis with no attendable event.
  Each independently attendable event/session is a separate item containing ALL keys:
  - event_title (nonempty string; keep concise)
  - provider_name (organizing host string, or null if unknown)
  - event_description (0–60 words; 1–2 concise event-only sentences, or empty; no warnings or AI commentary)
  - location (string or null)
  - start (object or null): year, month, day, hour, minute, timezone_iana
  - end (same shape or null)
  - duration_minutes (nonnegative integer or null)
  - confidence ("low", "medium", "high", or null)
  Optional session keys: duration (human-readable string), raw_notes.
- description (0–60 word event-only post context, including when events is empty; no warnings or AI commentary)
- raw_notes (string or null): ambiguities and missing information
- main_image_url (one of the loaded URLs or null): one representative image for the POST.
Do NOT merge separate sessions into one first-start-to-last-end time window.
For example, Coffee Chats 10:00–12:00 and Tech Talk 17:00–18:30 on the same day
must be TWO items, with durations 120 and 90 minutes, not a single 10:00–18:30 event.
Do not repeat an event merely because it appears in both caption and flyer slides.
Use null for unknown date/time components; do not invent a midnight time.
Use the supplied reference timestamp to resolve relative dates; never invent a year.
"""
    url_block = ""
    if loaded_image_urls:
        lines = "\n".join(f"  - {u}" for u in loaded_image_urls)
        url_block = (
            "\nImage URLs loaded for analysis (main_image_url must be one of these or null):\n"
            f"{lines}\n"
        )
    return (
        f"{schema}\n\n{EVIDENCE_INSTRUCTION}\n{image_evidence_note(post, loaded_image_urls)}\n\n"
        f"{ref_line}\n\n"
        f"from_username: {user}\n"
        f"shortcode: {shortcode}\n"
        f"permalink: {link}\n"
        f"title: {title}\n"
        f"Instagram caption (complementary evidence): {caption}\n"
        f"{text_block}"
        f"{img_note}"
        f"{url_block}"
    )


def _parse_json_text(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


_RETRY_AFTER_RE = re.compile(r"Please retry in\s+([\d.]+)\s*s", re.IGNORECASE)


def _client_error_blob(e: ClientError) -> str:
    d = e.details
    if isinstance(d, (dict, list)):
        try:
            return json.dumps(d, default=str)
        except (TypeError, ValueError):
            return str(d)
    return str(d or "")


def _retry_delay_from_client_error(e: ClientError) -> float:
    # for hay in (e.message or "", _client_error_blob(e)):
    #     m = _RETRY_AFTER_RE.search(hay)
    #     if m:
    #         try:
    #             return min(120.0, max(1.0, float(m.group(1))))
    #         except ValueError:
    #             break
    return 5.0


def _generate_content_with_transient_retry(
    client: genai.Client,
    model: str,
    contents: list[Any],
    config: types.GenerateContentConfig,
) -> Any:
    """Retry temporary server failures on the same request; leave quota handling to caller."""
    for attempt in range(3):
        try:
            diagnostic.phase('gemini_request')
            request_budget = http_budget.current()
            if request_budget is not None:
                request_budget.check_available()
            durable_attempt = attempt_safety.current()
            if durable_attempt is not None:
                durable_attempt.check_available()
                from app.parent_pipeline.daily_budget import reserve
                reserve('gemini', durable_attempt.parent_id)
            if durable_attempt is not None:
                durable_attempt.before_dispatch(model=model)
            if request_budget is not None:
                request_budget.reserve()
            diagnostic.event('request_started',model=model)
            from app.parent_pipeline.execution_log import paid_request_started
            paid_request_started('gemini')
            try:
                response = client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as exc:
                diagnostic.event('request_failed',exc,http_response=isinstance(exc,(ClientError,ServerError)))
                if durable_attempt is not None:
                    durable_attempt.outcome('failed' if isinstance(exc,(ClientError,ServerError)) else 'uncertain')
                if request_budget is not None and request_budget.strict:
                    request_budget.stop_failed_request(response_received=isinstance(exc,(ClientError,ServerError)))
                if durable_attempt is not None:
                    raise attempt_safety.GeminiReviewRequired() from None
                raise
            diagnostic.event('response_received')
            if durable_attempt is not None:
                durable_attempt.outcome('response_received')
            return response
        except ServerError as e:
            if e.code not in (500, 502, 503, 504):
                raise
            if attempt == 2:
                print(
                    f"Gemini API: model {model!r}: HTTP {e.code} after 3 attempts. "
                    "Giving up on this model's request.",
                    file=sys.stderr,
                )
                raise
            delay = 2.0 ** (attempt + 1)
            print(
                f"Gemini API: model {model!r}: HTTP {e.code} (temporary server error). "
                f"Waiting {delay:.1f}s, then retry {attempt + 2}/3 for the same post...",
                file=sys.stderr,
            )
            time.sleep(delay)


def _generate_content_with_429_retry(
    client: genai.Client,
    model: str,
    contents: list[Any],
    config: types.GenerateContentConfig,
    *,
    max_quota_retries: int = 5,
) -> Any:
    for attempt in range(max_quota_retries):
        try:
            return _generate_content_with_transient_retry(
                client, model, contents, config,
            )
        except ClientError as e:
            if e.code != 429:
                raise
            blob = _client_error_blob(e)
            if "limit: 0" in blob and "free_tier" in blob.lower():
                raise RuntimeError(
                    "Gemini API: free-tier quota exhausted for this model (limit 0). "
                    "Enable billing, set GEMINI_MODEL to a model with quota, or retry after reset. "
                    "See https://ai.google.dev/gemini-api/docs/rate-limits"
                ) from e
            if attempt == max_quota_retries - 1:
                print(
                    f"Gemini API: model {model!r}: 429 after {max_quota_retries} attempts "
                    f"(rate limit / quota). Giving up on this model.",
                    file=sys.stderr,
                )
                raise
            delay = _retry_delay_from_client_error(e)
            print(
                f"Gemini API: model {model!r}: 429 (rate limit / quota). "
                f"Waiting {delay:.1f}s, then retry {attempt + 2}/{max_quota_retries} on this model...",
                file=sys.stderr,
            )
            time.sleep(delay)


def format_analysis_error(e: BaseException) -> str:
    if isinstance(e, RuntimeError):
        s = str(e)
        if s == ALL_MODELS_EXHAUSTED_MSG:
            return s
        if s.startswith("Gemini API:"):
            return s.split("\n")[0][:500]
    if isinstance(e, ClientError):
        msg = (e.message or "").strip()
        if e.code == 429:
            return (
                f"429 RESOURCE_EXHAUSTED: {msg[:350]}…"
                if len(msg) > 350
                else f"429 RESOURCE_EXHAUSTED: {msg}"
            )
        tail = f"{msg[:400]}…" if len(msg) > 400 else msg
        return f"{e.code} {e.status or ''}: {tail}".strip()
    s = str(e)
    return s if len(s) <= 500 else s[:500] + "…"


def _clip_one_line(s: str, max_len: int) -> str:
    t = " ".join((s or "").split())
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


def _cdn_url_match_key(url: str) -> str:
    """Compare CDN URLs ignoring query/fragment and trivial scheme/host casing."""
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u)
    scheme = (parsed.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _resolve_main_image_url(
    cand_raw: str | None,
    loaded: list[str],
    *,
    shortcode_label: str,
) -> tuple[str | None, str | None]:
    """Pick canonical main_image_url from model output and loaded URLs.

    Returns (url_or_none, extra_raw_notes_line). When multiple images were loaded, we always
    return a non-null URL (first in carousel order as feed-safe default if the model omits or
    picks an unmatched string). stderr logs normalization and fallback paths.
    """
    if not loaded:
        return None, None
    if len(loaded) == 1:
        return loaded[0], None

    cand = (cand_raw or "").strip()
    allowed_set = set(loaded)
    if cand in allowed_set:
        return cand, None

    key_to_canonical: dict[str, str] = {}
    for u in loaded:
        k = _cdn_url_match_key(u)
        if k and k not in key_to_canonical:
            key_to_canonical[k] = u

    if cand:
        kc = _cdn_url_match_key(cand)
        if kc and kc in key_to_canonical:
            chosen = key_to_canonical[kc]
            if chosen != cand:
                msg = "main_image_url matched a loaded image after URL normalization (model string differed)."
                print(f"  AI [{shortcode_label}]: {msg}", file=sys.stderr)
                return chosen, msg
            return chosen, None

    fallback = loaded[0]
    if cand:
        msg = (
            "main_image_url not in loaded set after normalization; "
            "defaulted to first loaded image (Instagram cover order)."
        )
    else:
        msg = "main_image_url omitted by model; defaulted to first loaded image (Instagram cover order)."
    print(f"  AI [{shortcode_label}]: {msg}", file=sys.stderr)
    return fallback, msg


def format_ai_terminal_summary(shortcode: str, ai: PostAnalysisAI) -> str:
    """Display each session rather than a misleading aggregate window."""
    heading = f"  AI [{shortcode or '?'}] model={ai.gemini_model or '?'}: {len(ai.events)} event(s)"
    lines = [heading]
    for event in ai.events:
        start = event.start.model_dump(exclude_none=True) if event.start else None
        end = event.end.model_dump(exclude_none=True) if event.end else None
        lines.append(
            f"    {event.event_title} | provider={event.provider_name or '?'} | "
            f"start={start} | end={end} | duration={event.duration_minutes} min | "
            f"location={event.location or '?'} | confidence={event.confidence or '?'}"
        )
    if not ai.events:
        lines.append(f"    not an event — {_clip_one_line(ai.description, 110)}")
    return "\n".join(lines)


def _generation_config(model: str) -> types.GenerateContentConfig:
    # Gemini 3.6+ (including 3.8 and newer major versions) ignores/deprecates
    # sampling parameters. Keep the existing value for older/unknown families.
    version = re.match(r"gemini-(\d+)(?:\.(\d+))?(?:-|$)", model.rsplit("/", 1)[-1].lower())
    deprecated = version is not None and (int(version[1]), int(version[2] or 0)) >= (3, 6)
    sampling = {} if deprecated else {"temperature": 0.2}
    return types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        **sampling,
    )


def analyze_post(
    client: genai.Client,
    model_chain: list[str],
    post: dict[str, Any],
    tz: ZoneInfo,
    *,
    start_idx: int = 0,
    require_first_original: bool = False,
    before_request: Any = None,
) -> tuple[PostAnalysisAI, int]:
    """Walk model_chain from start_idx: bounded quota/transient retries then pass down; last failure → ALL_MODELS_EXHAUSTED.

    Returns (analysis, index_in_chain_of_model_used).
    Opt-in require_first_original prevents caption-only execution; before_request
    permits a locked analysis-only caller to revalidate its approved revision.
    """
    diagnostic.phase('input_preparation')
    ref = _taken_at_reference_line(post.get("taken_at"), tz)
    urls = _image_urls_from_post(post)
    if require_first_original and (not urls or urls[0] != post.get("first_image_url")):
        raise ValueError("Required first original image unavailable")
    image_parts: list[types.Part] = []
    loaded_image_urls: list[str] = []
    loaded_image_sizes: list[int] = []
    fetch_notes: list[str] = []
    for u in urls:
        diagnostic.phase('image_retrieval')
        got = _fetch_image(u)
        diagnostic.event('image_loaded' if got else 'image_unavailable')
        if got:
            data, mime = got
            if require_first_original and u == urls[0] and not data:
                raise ValueError("Required first original image unavailable")
            image_parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            loaded_image_urls.append(u)
            loaded_image_sizes.append(len(data))
        else:
            if require_first_original and u == urls[0]:
                raise ValueError("Required first original image unavailable")
            fetch_notes.append(f"failed to fetch image: {u[:80]}…")

    diagnostic.phase('input_preparation')
    base_prompt = _user_prompt(post, ref, len(image_parts), loaded_image_urls)
    diagnostic.input_summary(loaded_image_sizes, len(base_prompt))

    last_err: Exception | None = None
    sc = post.get("shortcode")
    label = sc if isinstance(sc, str) else "?"

    n_chain = len(model_chain)
    if n_chain == 0:
        raise RuntimeError("Gemini model chain is empty.")
    idx0 = max(0, min(start_idx, n_chain - 1))

    for model_idx, model in enumerate(model_chain[idx0:], start=idx0):
        config = _generation_config(model)
        quota_try_next = False
        for attempt in range(3):
            _fix_parts = [
                "\n\nYour previous output was invalid. Output only valid JSON matching the schema. ",
                "Include the required events array and all per-session fields. "
                "description and each event_description must be concise, at most 60 words, and contain only source-supported event facts; no warnings, uncertainty commentary, verification advice or AI reasoning. Use raw_notes for concerns. "
                "Never merge independently attendable sessions into one time window.",
            ]
            if attempt > 0 and len(loaded_image_urls) > 1:
                _fix_parts.append(
                    " When multiple images were loaded, main_image_url must be exactly one of the "
                    "listed \"Image URLs loaded for analysis\" strings (character-for-character copy), "
                    "choosing the single best representative slide for the post."
                )
            fix = "" if attempt == 0 else "".join(_fix_parts)
            contents = [*image_parts, base_prompt + fix]
            try:
                if before_request is not None:
                    before_request()
                resp = _generate_content_with_429_retry(
                    client,
                    model,
                    contents,
                    config,
                )
                diagnostic.phase('response_parsing_validation')
                raw = (resp.text or "").strip()
                data = _parse_json_text(raw)
                ai = PostAnalysisAI.model_validate(data)
                ai = constrain_sources(ai, post, loaded_image_urls, bool(_caption_text_from_post(post)))
                ai_to_child_event_values(ai, tz)
                notes = (ai.raw_notes or "").strip()
                upd: dict[str, Any] = {"gemini_model": model}
                final_main, main_note = _resolve_main_image_url(
                    ai.main_image_url,
                    loaded_image_urls,
                    shortcode_label=label,
                )
                raw_parts: list[str] = [s for s in (notes, main_note) if s]
                if fetch_notes:
                    raw_parts.append("Images: " + "; ".join(fetch_notes))
                if raw_parts:
                    upd["raw_notes"] = "\n".join(raw_parts)
                upd["main_image_url"] = final_main
                ai = ai.model_copy(update=upd)
                return ai, model_idx
            except ServerError as e:
                # Same-model transient retries are already exhausted by the helper.
                # Do not enter JSON repair retries or restart the post attempt.
                if e.code not in (500, 502, 503, 504):
                    raise
                if model_idx < len(model_chain) - 1:
                    print(
                        f"Gemini [{label}]: Temporary HTTP {e.code} retries exhausted for model {model!r}. "
                        f"Passing down the fallback chain → {model_chain[model_idx + 1]!r} "
                        f"({model_idx + 2}/{len(model_chain)}).",
                        file=sys.stderr,
                    )
                    quota_try_next = True
                    break
                raise RuntimeError(ALL_MODELS_EXHAUSTED_MSG) from e
            except ClientError as e:
                if e.code != 429:
                    raise
                if model_idx < len(model_chain) - 1:
                    nxt = model_chain[model_idx + 1]
                    print(
                        f"Gemini [{label}]: Rate/quota retries exhausted for model {model!r}. "
                        f"Passing down the fallback chain → {nxt!r} ({model_idx + 2}/{len(model_chain)}).",
                        file=sys.stderr,
                    )
                    quota_try_next = True
                    break
                print(
                    f"Gemini [{label}]: Rate/quota retries exhausted for model {model!r}; "
                    f"end of fallback chain ({len(model_chain)} models). {ALL_MODELS_EXHAUSTED_MSG}",
                    file=sys.stderr,
                )
                raise RuntimeError(ALL_MODELS_EXHAUSTED_MSG) from e
            except RuntimeError as e:
                msg = str(e)
                if "free-tier quota exhausted" in msg:
                    if model_idx < len(model_chain) - 1:
                        nxt = model_chain[model_idx + 1]
                        print(
                            f"Gemini [{label}]: Free-tier quota exhausted for model {model!r}. "
                            f"Passing down the fallback chain → {nxt!r} ({model_idx + 2}/{len(model_chain)}).",
                            file=sys.stderr,
                        )
                        quota_try_next = True
                        break
                    print(
                        f"Gemini [{label}]: Free-tier quota exhausted for model {model!r}; "
                        f"end of fallback chain ({len(model_chain)} models). {ALL_MODELS_EXHAUSTED_MSG}",
                        file=sys.stderr,
                    )
                    raise RuntimeError(ALL_MODELS_EXHAUSTED_MSG) from e
                raise
            except (json.JSONDecodeError, ValueError, ValidationError) as e:
                diagnostic.event('validation_failed',e)
                durable_attempt = attempt_safety.current()
                if durable_attempt is not None:
                    durable_attempt.outcome('validation_failed')
                    request_budget = http_budget.current()
                    if request_budget is not None:
                        request_budget.check_available()
                    raise attempt_safety.GeminiReviewRequired() from None
                last_err = e
                err_one = format_analysis_error(e)
                if attempt < 2:
                    print(
                        f"Gemini [{label}] model={model!r}: invalid JSON or schema ({type(e).__name__}: {err_one}). "
                        f"Retrying on this model with reminder ({attempt + 2}/3)...",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Gemini [{label}] model={model!r}: invalid JSON or schema on last try "
                        f"({type(e).__name__}: {err_one}).",
                        file=sys.stderr,
                    )
                time.sleep(0.5 * (attempt + 1))
        if quota_try_next:
            continue
        raise RuntimeError(f"Gemini JSON validation failed after retries: {last_err}") from last_err

