"""Firefox Instagram cookie export at repo root → Playwright cookies (PROJECT_ROOT, load helper)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_COOKIES = PROJECT_ROOT / "instagram_cookies_firefox.json"
_SAMESITE = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None"}


def _domain(host_raw: str) -> str:
    if not host_raw.startswith("http"):
        host_raw = "https://" + host_raw.lstrip("/")
    n = urlparse(host_raw).netloc
    if not n:
        return ".instagram.com"
    return n if n.startswith(".") else f".{n}"


def load_instagram_cookies_for_playwright() -> list[dict]:
    if not _COOKIES.is_file():
        raise SystemExit(f"Missing {_COOKIES.name} at repo root.")
    raw = _COOKIES.read_text(encoding="utf-8").strip()
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        raw = raw[1:-1]
    raw = re.sub(r"\\\\054|\\054", ",", raw)
    rows = json.loads(raw)
    out: list[dict] = []
    for c in rows:
        name, value = c.get("Name raw") or c.get("name"), c.get("Content raw") or c.get("value")
        if not name or value is None:
            continue
        if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        try:
            exp = float(c.get("Expires raw") or 0)
        except ValueError:
            exp = 0.0
        e: dict = {
            "name": name,
            "value": value,
            "domain": _domain(c.get("Host raw") or ""),
            "path": c.get("Path raw") or "/",
            "secure": str(c.get("Send for raw", "")).lower() == "true",
            "httpOnly": str(c.get("HTTP only raw", "")).lower() == "true",
        }
        if exp > 0:
            e["expires"] = exp
        if ss := _SAMESITE.get(str(c.get("SameSite raw", "")).lower().strip()):
            e["sameSite"] = ss
        out.append(e)
    if not out:
        raise SystemExit(f"No cookies parsed from {_COOKIES.name}.")
    return out
