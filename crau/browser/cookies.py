"""Cookie and header files.

Cookies: Netscape `cookies.txt` (curl, wget, yt-dlp, browser extensions) or JSON (a list of cookies, or an object with
a `cookies` list such as a Playwright storage state). Headers: a JSON object or `Name: value` lines.
Internally a cookie is a dict with `name`, `value`, `domain`, `path`, `secure`, `http_only`, `expires`, `same_site`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_HTTP_ONLY_PREFIX = "#HttpOnly_"


def normalize_cookie(raw: dict[str, Any]) -> dict[str, Any]:
    if "name" not in raw or "value" not in raw:
        raise ValueError(f"Cookie without name/value: {raw!r}")
    if not raw.get("domain") and not raw.get("url"):
        raise ValueError(f"Cookie {raw['name']!r} needs a domain or a url")
    expires = raw.get("expires", raw.get("expirationDate", raw.get("expiry")))
    cookie = {
        "name": str(raw["name"]),
        "value": str(raw["value"]),
        "domain": raw.get("domain") or "",
        "path": raw.get("path") or "/",
        "secure": bool(raw.get("secure", False)),
        "http_only": bool(raw.get("http_only", raw.get("httpOnly", False))),
        "expires": float(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
        "same_site": raw.get("same_site", raw.get("sameSite")) or None,
    }
    if raw.get("url"):
        cookie["url"] = raw["url"]
    return cookie


def parse_netscape(text: str) -> list[dict[str, Any]]:
    cookies = []
    for number, line in enumerate(text.splitlines(), start=1):
        http_only = line.startswith(_HTTP_ONLY_PREFIX)
        if http_only:
            line = line[len(_HTTP_ONLY_PREFIX) :]
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"cookies.txt line {number}: expected 7 tab-separated fields, got {len(fields)}")
        domain, _, path, secure, expires, name, value = fields
        cookies.append(
            normalize_cookie(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure.upper() == "TRUE",
                    "http_only": http_only,
                    "expires": int(expires) if expires.strip().isdigit() else None,
                }
            )
        )
    return cookies


def load_cookies(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        items = data.get("cookies", []) if isinstance(data, dict) else data
        return [normalize_cookie(item) for item in items]
    return parse_netscape(text)


def dump_netscape(cookies: list[dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = cookie["domain"]
        lines.append(
            "\t".join(
                [
                    (_HTTP_ONLY_PREFIX if cookie.get("http_only") else "") + domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    cookie.get("path") or "/",
                    "TRUE" if cookie.get("secure") else "FALSE",
                    str(int(cookie["expires"])) if cookie.get("expires") else "0",
                    cookie["name"],
                    cookie["value"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def parse_header(text: str) -> tuple[str, str]:
    """`Name: value` to a pair (used as an argparse type, hence ValueError)."""
    name, separator, value = text.partition(":")
    if not separator or not name.strip() or " " in name.strip():
        raise ValueError(f"Invalid header {text!r} (expected 'Name: value')")
    return name.strip(), value.strip()


def load_headers(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        return {str(name): str(value) for name, value in json.loads(text).items()}
    headers = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, value = parse_header(line)
            headers[name] = value
    return headers
