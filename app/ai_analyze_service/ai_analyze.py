"""
Classify Instagram posts (event vs not) and extract structured fields via Google Gemini.

Environment (set in .env in project root, or .env.gemini):
  GEMINI_API_KEY or GOOGLE_API_KEY — required for the Gemini API.
  GEMINI_MODEL — optional; default gemini-2.5-flash (stable; 2.0 Flash is deprecated).
  GEMINI_MODEL_FALLBACK — optional comma-separated model ids tried after GEMINI_MODEL when
    429/free-tier quota is exhausted (default: see DEFAULT_MODEL_FALLBACK_CHAIN in code).
  GEMINI_DEFAULT_TZ — optional IANA zone for interpreting relative dates (default America/Los_Angeles).

.env format: simple KEY=value lines (optional "export " prefix, # comments). A large
python-dotenv-incompatible .env is parsed line-by-line here so you do not get hundreds
of parse warnings. Put Gemini keys in .env.gemini if you prefer a tiny file.

Usage:
  python -m app.ai_analyze_service.ai_analyze
  python -m app.ai_analyze_service.ai_analyze --profile seventhcollegestudentcouncil --limit 3
  python -m app.ai_analyze_service.ai_analyze --resume  # skip posts that already have ai in posts_ai.json

Model fallback chain (rate/quota):
  We start with GEMINI_MODEL. Each model gets up to several HTTP 429 retries; when that model’s
  retry budget is exhausted, we pass down to the next id in the chain (GEMINI_MODEL_FALLBACK or
  DEFAULT_MODEL_FALLBACK_CHAIN), in order, until one succeeds or every model is exhausted — then we
  stop with "We ran out of all models." Invalid-JSON repair attempts stay on the same model and do
  not advance the chain.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# Repo root (…/playwright-Playground); file is app/ai_analyze_service/ai_analyze.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMP_DOWNLOAD = PROJECT_ROOT / "temp_download"


def _parse_env_file(path: Path) -> None:
    """Merge KEY=value lines into os.environ. Skips invalid lines silently (no python-dotenv)."""
    import os

    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val


def _load_env() -> None:
    _parse_env_file(PROJECT_ROOT / ".env")
    _parse_env_file(PROJECT_ROOT / ".env.gemini")


_load_env()

# Stable model ids; see https://ai.google.dev/gemini-api/docs/models
# Order: primary (GEMINI_MODEL) then these, deduped. All are commonly available on the free tier;
# exact quotas vary by model and region—check AI Studio rate limits.
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_MODEL_FALLBACK_CHAIN: tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)

# Raised when every model in the chain hits rate/quota or free-tier exhaustion.
ALL_MODELS_EXHAUSTED_MSG = "We ran out of all models."
DEFAULT_TZ = "America/Los_Angeles"
MAX_IMAGES = 6
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_SYSTEM_INSTRUCTION = """You analyze Instagram posts for a campus/student-audience feed.
Return a single JSON object matching the user's schema. No markdown, no code fences.

Event vs not:
- An EVENT is a scheduled happening with a specific date/time window people can attend
  (workshops, fairs, meetings, performances, deadlines for in-person activities, etc.).
- NOT an event: general announcements, tips, memes, ticket-sale windows without the main event
  date in the post, reposts that are purely promotional text, or vague content with no
  schedulable occurrence.

Rules:
- Use the post's title, caption, optional thread comments, and any attached images (flyers).
- Resolve relative dates ("this Saturday", "tomorrow") using the reference datetime provided.
- If you cannot resolve year/month/day or time from the text, use null for those fields and
  explain in raw_notes. Do not invent a year.
- For events, provider_name is the organizing host (student council, department, etc.); use
  from_username only if no clearer host is named.
- duration should be human-readable when possible (e.g. "7:00 PM – 9:00 PM"). Include
  duration_minutes when you can compute it.
- Times: use 24-hour fields in start/end (hour 0-23, minute 0-59). If only 12h text is given,
  convert and note ambiguity in raw_notes.
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
    # Set by the analyzer after a successful API call (not returned by Gemini JSON).
    gemini_model: Optional[str] = None

    @model_validator(mode="after")
    def _validate_branch(self) -> PostAnalysisAI:
        if self.is_event:
            if not (self.event_title or "").strip():
                raise ValueError("event_title is required when is_event is true")
            if not (self.provider_name or "").strip():
                raise ValueError("provider_name is required when is_event is true")
        else:
            if not (self.description or "").strip():
                raise ValueError("description is required when is_event is false")
        return self


def _gemini_api_key() -> str:
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


def _gemini_model_chain() -> list[str]:
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


def _default_tz() -> ZoneInfo:
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


def _user_prompt(post: dict[str, Any], ref_line: str, num_images: int) -> str:
    title = post.get("title") or ""
    caption = (post.get("caption") or post.get("comment") or "").strip()
    user = post.get("from_username") or ""
    link = post.get("permalink") or ""
    shortcode = post.get("shortcode") or ""
    comments_block = ""
    raw_comments = post.get("comments")
    if isinstance(raw_comments, list) and raw_comments:
        lines: list[str] = []
        for i, c in enumerate(raw_comments, start=1):
            if not isinstance(c, dict):
                continue
            who = (c.get("username") or "?").strip()
            body = (c.get("text") or "").strip()
            lines.append(f"{i}. @{who}: {body}")
        if lines:
            comments_block = "thread_comments:\n" + "\n".join(lines) + "\n\n"
    img_note = (
        f"{num_images} image(s) are attached after this text (flyer/carousel)."
        if num_images
        else "No images could be loaded; use title and caption only."
    )
    schema = """
Respond with JSON only, with these keys:
- is_event (boolean)
If is_event is true:
  - event_title (string)
  - provider_name (string)
  - start (object or null): year, month, day, hour, minute, timezone?, timezone_iana?, assumptions_note?
  - end (object or null): same shape as start; null if unknown
  - duration (string or null): e.g. "7:00 PM – 9:00 PM"
  - duration_minutes (integer or null)
  - event_description (string or null): short summary of the activity
If is_event is false:
  - description (string): what this post is about
Always include when possible:
  - confidence: "low" | "medium" | "high"
  - raw_notes (string or null): ambiguities or missing info
"""
    return (
        f"{schema}\n\n"
        f"{ref_line}\n\n"
        f"from_username: {user}\n"
        f"shortcode: {shortcode}\n"
        f"permalink: {link}\n"
        f"title: {title}\n"
        f"caption:\n{caption}\n\n"
        f"{comments_block}"
        f"{img_note}"
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


def _format_analysis_error(e: BaseException) -> str:
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


def _format_ai_terminal_summary(shortcode: str, ai: PostAnalysisAI) -> str:
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
        body = " | ".join(parts)
        return f"  AI [{sc}] model={mod} {conf}: event — {body}"
    desc = _clip_one_line(ai.description or "", 110)
    return f"  AI [{sc}] model={mod} {conf}: not an event — {desc}"


def _analyze_post(
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
    fetch_notes: list[str] = []
    for u in urls:
        got = _fetch_image(u)
        if got:
            data, mime = got
            image_parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        else:
            fetch_notes.append(f"failed to fetch image: {u[:80]}…")

    base_prompt = _user_prompt(post, ref, len(image_parts))

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
            fix = (
                ""
                if attempt == 0
                else "\n\nYour previous output was invalid. Output only valid JSON matching the schema."
            )
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
                if fetch_notes:
                    extra = "Images: " + "; ".join(fetch_notes)
                    upd["raw_notes"] = f"{notes}\n{extra}".strip() if notes else extra
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
                err_one = _format_analysis_error(e)
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


def _discover_profiles(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "posts.json").is_file():
            out.append(child)
    return out


def _load_existing_ai_by_shortcode(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    m: dict[str, dict[str, Any]] = {}
    for p in data.get("posts") or []:
        sc = p.get("shortcode")
        if isinstance(sc, str) and "ai" in p and isinstance(p["ai"], dict):
            m[sc] = p["ai"]
    return m


def process_profile_dir(
    clean_dir: Path,
    *,
    client: genai.Client,
    model_chain: list[str],
    tz: ZoneInfo,
    limit: int | None,
    resume: bool,
    sleep_s: float,
    chain_start_idx: int = 0,
) -> int:
    posts_path = clean_dir / "posts.json"
    out_path = clean_dir / "posts_ai.json"
    bundle = json.loads(posts_path.read_text(encoding="utf-8"))
    posts = bundle.get("posts")
    if not isinstance(posts, list):
        print(f"Skip {clean_dir.name}: invalid posts.json", file=sys.stderr)
        return chain_start_idx

    existing_ai = _load_existing_ai_by_shortcode(out_path) if resume else {}
    analyzed = 0
    out_posts: list[dict[str, Any]] = []
    chain_offset = chain_start_idx

    for post in posts:
        if not isinstance(post, dict):
            continue
        sc = post.get("shortcode")
        if resume and isinstance(sc, str) and sc in existing_ai:
            merged = dict(post)
            merged["ai"] = existing_ai[sc]
            out_posts.append(merged)
            continue
        if limit is not None and analyzed >= limit:
            out_posts.append(dict(post))
            continue
        try:
            ai, used_idx = _analyze_post(
                client, model_chain, post, tz, start_idx=chain_offset
            )
            chain_offset = used_idx
            row = dict(post)
            row["ai"] = ai.model_dump(mode="json", exclude_none=False)
            out_posts.append(row)
            analyzed += 1
            sc_str = sc if isinstance(sc, str) else "?"
            print(_format_ai_terminal_summary(sc_str, ai))
            if sleep_s > 0:
                time.sleep(sleep_s)
        except Exception as e:
            brief = _format_analysis_error(e)
            print(f"  Error {sc}: {brief}", file=sys.stderr)
            row = dict(post)
            row["ai"] = {
                "error": brief,
                "is_event": False,
                "description": f"Analysis failed: {brief}",
                "confidence": "low",
            }
            out_posts.append(row)
            analyzed += 1
            if sleep_s > 0:
                time.sleep(sleep_s)

    out_bundle = {
        "scraped_profile": bundle.get("scraped_profile", clean_dir.name),
        "posts": out_posts,
    }
    out_path.write_text(
        json.dumps(out_bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)} ({len(out_posts)} posts, analyzed {analyzed})")
    return chain_offset


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Instagram posts.json with Gemini.")
    parser.add_argument(
        "--profile",
        help="Only process temp_download/<profile>/ (default: all profiles with posts.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Analyze at most this many posts per profile (others copied without new ai)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing posts_ai.json ai blocks by shortcode; only analyze missing",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=3.0,
        help="Seconds to sleep between Gemini calls (default 3.0)",
    )
    args = parser.parse_args()

    key = _gemini_api_key()
    model_chain = _gemini_model_chain()
    tz = _default_tz()
    client = genai.Client(api_key=key)

    print(
        f"Gemini model chain ({len(model_chain)}): {' → '.join(model_chain)}",
        file=sys.stderr,
    )

    if args.profile:
        dirs = [TEMP_DOWNLOAD / args.profile.strip().lstrip("@")]
        if not (dirs[0] / "posts.json").is_file():
            print(f"No posts.json at {dirs[0]}", file=sys.stderr)
            sys.exit(1)
    else:
        dirs = _discover_profiles(TEMP_DOWNLOAD)

    if not dirs:
        print(f"No profiles with posts.json under {TEMP_DOWNLOAD}", file=sys.stderr)
        sys.exit(1)

    chain_offset = 0
    for d in dirs:
        print(f"Profile: {d.name}")
        chain_offset = process_profile_dir(
            d,
            client=client,
            model_chain=model_chain,
            tz=tz,
            limit=args.limit,
            resume=args.resume,
            sleep_s=args.sleep,
            chain_start_idx=chain_offset,
        )


if __name__ == "__main__":
    main()
