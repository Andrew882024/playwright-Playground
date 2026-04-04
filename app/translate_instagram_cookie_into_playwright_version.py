"""
Load Instagram cookies from .env (Firefox export JSON) and convert them to Playwright cookie dicts.

Does not open a browser — use sync_ins.py for that.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

# Repo root when this file lives in app/ (…/playwright-Playground/app/this_file.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_firefox_cookie_json(raw: str) -> list[dict]:
    """Parse Firefox cookie export; repair invalid JSON escapes (e.g. \\054 for comma)."""
    raw = raw.strip()
    if (raw.startswith("'") and raw.endswith("'")) or (
        raw.startswith('"') and raw.endswith('"')
    ):
        raw = raw[1:-1]
    raw = re.sub(r"\\\\054", ",", raw)
    raw = re.sub(r"\\054", ",", raw)
    return json.loads(raw)


def _host_raw_to_domain(host_raw: str) -> str:
    """e.g. https://.instagram.com/ -> .instagram.com"""
    if not host_raw.startswith("http"):
        host_raw = "https://" + host_raw.lstrip("/")
    netloc = urlparse(host_raw).netloc
    if not netloc:
        return ".instagram.com"
    return netloc if netloc.startswith(".") else f".{netloc}"


def _same_site_to_playwright(s: str) -> str | None:
    s = (s or "").lower().strip()
    if s in ("lax",):
        return "Lax"
    if s in ("strict",):
        return "Strict"
    if s in ("none", "no_restriction"):
        return "None"
    return None


def firefox_export_to_playwright_cookies(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in rows:
        name = c.get("Name raw") or c.get("name")
        value = c.get("Content raw") or c.get("value")
        if not name or value is None:
            continue
        if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        host_raw = c.get("Host raw") or ""
        domain = _host_raw_to_domain(host_raw)
        path = c.get("Path raw") or "/"
        expires_raw = c.get("Expires raw") or "0"
        try:
            exp = float(expires_raw)
        except ValueError:
            exp = 0.0
        secure = str(c.get("Send for raw", "")).lower() == "true"
        http_only = str(c.get("HTTP only raw", "")).lower() == "true"
        same_site = _same_site_to_playwright(str(c.get("SameSite raw", "")))

        entry: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": secure,
            "httpOnly": http_only,
        }
        if exp > 0:
            entry["expires"] = exp
        if same_site is not None:
            entry["sameSite"] = same_site
        out.append(entry)
    return out


def _parse_cookie_json_from_env_file(env_path: Path, key: str) -> str:
    """Read multi-line value for KEY=... from .env."""
    text = env_path.read_text(encoding="utf-8")
    idx = text.find(key)
    if idx == -1:
        raise SystemExit(f"Missing {key} in {env_path}")
    j = idx + len(key)
    while j < len(text) and text[j] in " \t":
        j += 1
    if j >= len(text) or text[j] != "=":
        raise SystemExit(f"Expected = after {key} in {env_path}")
    j += 1
    rest = text[j:].lstrip("\n")
    m = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_]*\s*=", rest)
    if m and m.start() > 0:
        rest = rest[: m.start()].rstrip()
    rest = rest.strip()
    try:
        _load_firefox_cookie_json(rest)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"Could not parse {key} as JSON in {env_path}: {e}. "
            "Use INSTAGRAM_COOKIE_FILE pointing at a .json file if needed."
        ) from e
    return rest


def _read_scalar_env(env_path: Path, key: str) -> str | None:
    """Single-line KEY=value from .env."""
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(rf"^{re.escape(key)}\s*=", line):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _load_cookie_raw() -> str:
    env_path = PROJECT_ROOT / ".env"
    path_str = (
        os.environ.get("INSTAGRAM_COOKIE_FILE", "").strip()
        or (_read_scalar_env(env_path, "INSTAGRAM_COOKIE_FILE") or "")
    )
    if path_str:
        p = (PROJECT_ROOT / path_str).resolve()
        if p.is_file():
            return p.read_text(encoding="utf-8")
        raise SystemExit(f"INSTAGRAM_COOKIE_FILE not found: {p}")

    raw = os.environ.get("INSTAGRAM_RAW_COOKIE_FROM_FIREFOX", "").strip()
    if raw.startswith("[") and len(raw) > 10:
        try:
            _load_firefox_cookie_json(raw)
            return raw
        except json.JSONDecodeError:
            pass

    if env_path.is_file():
        return _parse_cookie_json_from_env_file(env_path, "INSTAGRAM_RAW_COOKIE_FROM_FIREFOX")

    raise SystemExit(
        "Missing INSTAGRAM_RAW_COOKIE_FROM_FIREFOX in .env (Firefox cookie JSON), "
        "or INSTAGRAM_COOKIE_FILE."
    )


def load_instagram_cookies_for_playwright() -> list[dict]:
    """Read .env / cookie file, parse Firefox export, return Playwright-ready cookie list."""
    raw = _load_cookie_raw()
    rows = _load_firefox_cookie_json(raw)
    cookies = firefox_export_to_playwright_cookies(rows)
    if not cookies:
        raise SystemExit("No cookies parsed; check INSTAGRAM_RAW_COOKIE_FROM_FIREFOX format.")
    return cookies


def read_instagram_target_url() -> str:
    """Target URL from env or .env (optional INSTAGRAM_TARGET_URL)."""
    env_path = PROJECT_ROOT / ".env"
    return (
        os.environ.get("INSTAGRAM_TARGET_URL", "").strip()
        or (_read_scalar_env(env_path, "INSTAGRAM_TARGET_URL") or "")
        or "https://www.instagram.com/"
    )


def read_instagram_profile_username() -> str | None:
    """Instagram handle (no @) from env or .env (optional INSTAGRAM_PROFILE_USERNAME)."""
    env_path = PROJECT_ROOT / ".env"
    u = (
        os.environ.get("INSTAGRAM_PROFILE_USERNAME", "").strip()
        or (_read_scalar_env(env_path, "INSTAGRAM_PROFILE_USERNAME") or "")
    )
    u = u.lstrip("@").strip()
    return u or None
