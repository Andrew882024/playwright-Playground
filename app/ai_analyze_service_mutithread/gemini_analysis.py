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

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

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


class ModelQuotaExhausted(RuntimeError):
    """This model hit rate/quota limits after retries; worker should reassign to the next queue."""
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
- Use the post's title, comments array (caption + thread replies), and any attached images (flyers).
- Resolve relative dates ("this Saturday", "tomorrow") using the reference datetime provided.
- If you cannot resolve year/month/day or time from the text, use null for those fields and
  explain in raw_notes. Do not invent a year.
- For events, provider_name is the organizing host (student council, department, etc.); use
  from_username only if no clearer host is named.
- duration should be human-readable when possible (e.g. "7:00 PM – 9:00 PM"). Include
  duration_minutes when you can compute it.
- Times: use 24-hour fields in start/end (hour 0-23, minute 0-59). If only 12h text is given,
  convert and note ambiguity in raw_notes.
- For non-events, description must be prose; for events, event_description must be prose.
  Each must be 50–200 words (count whitespace-separated tokens), inclusive.
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


class DateTimeParts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
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


def _post_description_source_text(ai: PostAnalysisAI) -> str:
    if ai.is_event:
        return (ai.event_description or "").strip()
    return (ai.description or "").strip()


def _post_description_for_db(ai: PostAnalysisAI) -> str:
    """Persisted post_description: at most _POST_DESC_MAX_WORDS (validator enforces minimum on ingest)."""
    return _truncate_to_max_words(_post_description_source_text(ai), _POST_DESC_MAX_WORDS)


def _zone_for_datetime_parts(parts: DateTimeParts, default_tz: ZoneInfo) -> ZoneInfo:
    iana = (parts.timezone_iana or "").strip()
    if iana:
        try:
            return ZoneInfo(iana)
        except ZoneInfoNotFoundError:
            pass
    tz_hint = (parts.timezone or "").strip()
    if tz_hint and "/" in tz_hint:
        try:
            return ZoneInfo(tz_hint)
        except ZoneInfoNotFoundError:
            pass
    return default_tz


def _datetime_from_parts(
    parts: DateTimeParts | None,
    default_tz: ZoneInfo,
) -> datetime | None:
    """Full calendar date required (y/m/d); hour/minute default to 0. No invented year."""
    if parts is None:
        return None
    y, mo, d = parts.year, parts.month, parts.day
    if y is None or mo is None or d is None:
        return None
    h = 0 if parts.hour is None else int(parts.hour)
    mi = 0 if parts.minute is None else int(parts.minute)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    try:
        z = _zone_for_datetime_parts(parts, default_tz)
        return datetime(y, mo, d, h, mi, tzinfo=z)
    except (ValueError, TypeError, OSError):
        return None


class PostAnalysisAI(BaseModel):
    """Validated Gemini output for one post."""

    model_config = ConfigDict(extra="ignore")

    is_event: bool
    event_title: Optional[str] = None
    provider_name: Optional[str] = None
    description: Optional[str] = None
    start: Optional[DateTimeParts] = None
    end: Optional[DateTimeParts] = None
    duration: Optional[str] = None
    duration_minutes: Optional[int] = None
    event_description: Optional[str] = None
    confidence: Optional[Literal["low", "medium", "high"]] = None
    raw_notes: Optional[str] = None
    location: Optional[str] = None
    # Best cover image among those loaded for this request; set after validation in analyze_post.
    main_image_url: Optional[str] = None
    # Set by the analyzer after a successful API call (not returned by Gemini JSON).
    gemini_model: Optional[str] = None

    @model_validator(mode="after")
    def _validate_branch(self) -> PostAnalysisAI:
        if self.is_event:
            if not (self.event_title or "").strip():
                raise ValueError("event_title is required when is_event is true")
            if not (self.provider_name or "").strip():
                raise ValueError("provider_name is required when is_event is true")
            body = (self.event_description or "").strip()
            if not body:
                raise ValueError("event_description is required when is_event is true")
            wc = _word_count(body)
            if wc < _POST_DESC_MIN_WORDS or wc > _POST_DESC_MAX_WORDS:
                raise ValueError(
                    f"event_description must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words "
                    f"(got {wc})"
                )
        else:
            body = (self.description or "").strip()
            if not body:
                raise ValueError("description is required when is_event is false")
            wc = _word_count(body)
            if wc < _POST_DESC_MIN_WORDS or wc > _POST_DESC_MAX_WORDS:
                raise ValueError(
                    f"description must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words "
                    f"(got {wc})"
                )
        return self


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
    start_at = _datetime_from_parts(ai.start, default_tz) if ai.is_event else None
    end_at = _datetime_from_parts(ai.end, default_tz) if ai.is_event else None
    chosen = (ai.main_image_url or "").strip() if ai.main_image_url else ""
    fb = (fallback_main_image_url or "").strip() if fallback_main_image_url else ""
    main_url: str | None = chosen if chosen else (fb if fb else None)
    loc = (ai.location or "").strip()
    return {
        "is_event": ai.is_event,
        "event_title": (ai.event_title or "").strip() if ai.is_event else None,
        "provider_name": (ai.provider_name or "").strip() if ai.is_event else None,
        "post_description": _post_description_for_db(ai),
        "duration_in_minutes": ai.duration_minutes,
        "confidence": ai.confidence,
        "ai_model": ai.gemini_model,
        "ai_analyzed": True,
        "event_start_at": start_at,
        "event_end_at": end_at,
        "main_image_url": main_url,
        "location": loc if loc else None,
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
    for u in urls:
        if _is_probably_video(u):
            continue
        if not _looks_like_image_url(u):
            continue
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


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
    except (HTTPError, URLError, TimeoutError, OSError):
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
        else "No images could be loaded; use title and comments text only."
    )
    schema = f"""
Respond with JSON only, with these keys:
- is_event (boolean)
If is_event is true:
  - event_title (string)
  - provider_name (string)
  - start (object or null): year, month, day, hour, minute, timezone?, timezone_iana?, assumptions_note?
  - end (object or null): same shape as start; null if unknown
  - duration (string or null): e.g. "7:00 PM – 9:00 PM"
  - duration_minutes (integer or null)
  - event_description (string): prose summary of the activity for a student feed;
    must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words inclusive (count words as whitespace-separated tokens).
If is_event is false:
  - description (string): what this post is about;
    must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words inclusive (count words as whitespace-separated tokens).
Always include when possible:
  - confidence: "low" | "medium" | "high"
  - raw_notes (string or null): ambiguities or missing info
  - location (string or null): venue, building/room, campus, neighborhood, address, or Online / meeting link
    if clearly stated; otherwise null
  - main_image_url (string or null): when "Image URLs loaded for analysis" lists URLs below, set this
    to exactly one of those strings (copy verbatim) — the single image that best represents the post
    in a feed (events: clearest title/date/time/location on a flyer; non-events: strongest visual hook
    or clearest message; prefer legible text over decorative-only slides). If that list is empty, use null.
"""
    url_block = ""
    if loaded_image_urls:
        lines = "\n".join(f"  - {u}" for u in loaded_image_urls)
        url_block = (
            "\nImage URLs loaded for analysis (main_image_url must be one of these or null):\n"
            f"{lines}\n"
        )
    return (
        f"{schema}\n\n"
        f"{ref_line}\n\n"
        f"from_username: {user}\n"
        f"shortcode: {shortcode}\n"
        f"permalink: {link}\n"
        f"title: {title}\n"
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
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
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
    """Human-readable one-line summary of a successful analysis (stdout)."""
    conf = ai.confidence or "?"
    sc = shortcode if shortcode else "?"
    mod = ai.gemini_model or "?"
    if ai.is_event:
        parts = [
            f'title="{_clip_one_line(ai.event_title or "", 72)}"',
            f'provider="{_clip_one_line(ai.provider_name or "", 48)}"',
        ]
        if ai.duration:
            parts.append(f'when="{_clip_one_line(ai.duration, 40)}"')
        elif ai.start and ai.start.year and ai.start.month and ai.start.day:
            y, m, d = ai.start.year, ai.start.month, ai.start.day
            parts.append(f"start={y:04d}-{m:02d}-{d:02d}")
        if (ai.location or "").strip():
            parts.append(f'loc="{_clip_one_line(ai.location or "", 56)}"')
        body = " | ".join(parts)
        return f"  AI [{sc}] model={mod} {conf}: event — {body}"
    desc = _clip_one_line(ai.description or "", 110)
    return f"  AI [{sc}] model={mod} {conf}: not an event — {desc}"


def analyze_post(
    client: genai.Client,
    model_chain: list[str],
    post: dict[str, Any],
    tz: ZoneInfo,
    *,
    start_idx: int = 0,
) -> tuple[PostAnalysisAI, int]:
    """Walk model_chain from start_idx: per model, 429 retries then pass down; last failure → ALL_MODELS_EXHAUSTED.

    Returns (analysis, index_in_chain_of_model_used).
    """
    ref = _taken_at_reference_line(post.get("taken_at"), tz)
    urls = _image_urls_from_post(post)
    image_parts: list[types.Part] = []
    loaded_image_urls: list[str] = []
    fetch_notes: list[str] = []
    for u in urls:
        got = _fetch_image(u)
        if got:
            data, mime = got
            image_parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            loaded_image_urls.append(u)
        else:
            fetch_notes.append(f"failed to fetch image: {u[:80]}…")

    base_prompt = _user_prompt(post, ref, len(image_parts), loaded_image_urls)

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=0.2,
        response_mime_type="application/json",
    )

    last_err: Exception | None = None
    sc = post.get("shortcode")
    label = sc if isinstance(sc, str) else "?"

    n_chain = len(model_chain)
    if n_chain == 0:
        raise RuntimeError("Gemini model chain is empty.")
    idx0 = max(0, min(start_idx, n_chain - 1))

    for model_idx, model in enumerate(model_chain[idx0:], start=idx0):
        quota_try_next = False
        for attempt in range(3):
            _fix_parts = [
                "\n\nYour previous output was invalid. Output only valid JSON matching the schema. ",
                f"If is_event is false, description must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words; "
                f"if true, event_description must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words.",
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
                resp = _generate_content_with_429_retry(
                    client,
                    model,
                    contents,
                    config,
                )
                raw = (resp.text or "").strip()
                data = _parse_json_text(raw)
                ai = PostAnalysisAI.model_validate(data)
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


def analyze_post_single_model(
    client: genai.Client,
    model: str,
    post: dict[str, Any],
    tz: ZoneInfo,
) -> PostAnalysisAI:
    """Analyze one post with a single Gemini model (for per-model worker threads).

    Raises ModelQuotaExhausted when 429 / free-tier retries are exhausted on this model.
    """
    ref = _taken_at_reference_line(post.get("taken_at"), tz)
    urls = _image_urls_from_post(post)
    image_parts: list[types.Part] = []
    loaded_image_urls: list[str] = []
    fetch_notes: list[str] = []
    for u in urls:
        got = _fetch_image(u)
        if got:
            data, mime = got
            image_parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            loaded_image_urls.append(u)
        else:
            fetch_notes.append(f"failed to fetch image: {u[:80]}…")

    base_prompt = _user_prompt(post, ref, len(image_parts), loaded_image_urls)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=0.2,
        response_mime_type="application/json",
    )

    last_err: Exception | None = None
    sc = post.get("shortcode")
    label = sc if isinstance(sc, str) else "?"

    for attempt in range(3):
        _fix_parts = [
            "\n\nYour previous output was invalid. Output only valid JSON matching the schema. ",
            f"If is_event is false, description must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words; "
            f"if true, event_description must be {_POST_DESC_MIN_WORDS}–{_POST_DESC_MAX_WORDS} words.",
        ]
        if attempt > 0 and len(loaded_image_urls) > 1:
            _fix_parts.append(
                " When multiple images were loaded, main_image_url must be exactly one of the "
                'listed "Image URLs loaded for analysis" strings (character-for-character copy), '
                "choosing the single best representative slide for the post."
            )
        fix = "" if attempt == 0 else "".join(_fix_parts)
        contents = [*image_parts, base_prompt + fix]
        try:
            resp = _generate_content_with_429_retry(
                client,
                model,
                contents,
                config,
            )
            raw = (resp.text or "").strip()
            data = _parse_json_text(raw)
            ai = PostAnalysisAI.model_validate(data)
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
            return ai.model_copy(update=upd)
        except ClientError as e:
            if e.code == 429:
                print(
                    f"Gemini [{label}]: Rate/quota retries exhausted for model {model!r}.",
                    file=sys.stderr,
                )
                raise ModelQuotaExhausted(model) from e
            raise
        except RuntimeError as e:
            msg = str(e)
            if "free-tier quota exhausted" in msg:
                print(
                    f"Gemini [{label}]: Free-tier quota exhausted for model {model!r}.",
                    file=sys.stderr,
                )
                raise ModelQuotaExhausted(model) from e
            raise
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            err_one = format_analysis_error(e)
            if attempt < 2:
                print(
                    f"Gemini [{label}] model={model!r}: invalid JSON or schema "
                    f"({type(e).__name__}: {err_one}). Retrying ({attempt + 2}/3)...",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Gemini [{label}] model={model!r}: invalid JSON or schema on last try "
                    f"({type(e).__name__}: {err_one}).",
                    file=sys.stderr,
                )
            time.sleep(0.5 * (attempt + 1))

    raise RuntimeError(f"Gemini JSON validation failed after retries: {last_err}") from last_err

