"""
Load Instagram cookies from a JSON file (Firefox export) and convert them to Playwright cookie dicts.

Set INSTAGRAM_COOKIE_FILE in the environment or in `.env` (path relative to repo root).

Does not open a browser — use sync_ins.py for that.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

# Repo root: …/playwright-Playground/ (this file is app/instagram_scraper/…)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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
    """Load Firefox cookie export JSON from INSTAGRAM_COOKIE_FILE (required)."""
    env_path = PROJECT_ROOT / ".env"
    path_str = (
        os.environ.get("INSTAGRAM_COOKIE_FILE", "").strip()
        or (_read_scalar_env(env_path, "INSTAGRAM_COOKIE_FILE") or "")
    )
    if not path_str:
        raise SystemExit(
            "Set INSTAGRAM_COOKIE_FILE to a JSON file path (Firefox cookie export), "
            f"either in the environment or in {env_path} (path relative to repo root)."
        )
    p = (PROJECT_ROOT / path_str).resolve()
    if not p.is_file():
        raise SystemExit(f"INSTAGRAM_COOKIE_FILE not found: {p}")
    return p.read_text(encoding="utf-8")


def load_instagram_cookies_for_playwright() -> list[dict]:
    """Read cookie JSON file, parse Firefox export, return Playwright-ready cookie list."""
    raw = _load_cookie_raw()
    rows = _load_firefox_cookie_json(raw)
    cookies = firefox_export_to_playwright_cookies(rows)
    if not cookies:
        raise SystemExit("No cookies parsed; check INSTAGRAM_COOKIE_FILE JSON format.")
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
