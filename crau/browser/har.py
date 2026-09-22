"""HAR 1.2 output and a compact, value-free summary of a HAR file.

Nothing is filtered or redacted when writing a HAR: headers, cookies and bodies are saved as captured.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .network import NetworkRecord

_TEXT_MIME = re.compile(
    r"^(text/|application/(json|xml|javascript|x-javascript|xhtml\+xml|x-www-form-urlencoded)|image/svg)"
)
_TEXT_SUFFIX = ("+json", "+xml")
_HTTP_VERSIONS = {"h2": "HTTP/2", "h3": "HTTP/3", "http/1.1": "HTTP/1.1", "http/1.0": "HTTP/1.0"}


def _header(pairs: list[tuple[str, str]], name: str) -> str:
    return next((value for key, value in pairs if key.lower() == name), "")


def _http_version(protocol: str) -> str:
    return _HTTP_VERSIONS.get(protocol.lower(), protocol.upper() or "HTTP/1.1")


def _request_cookies(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    cookies = []
    for part in _header(pairs, "cookie").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator:
            cookies.append({"name": name, "value": value})
    return cookies


def _response_cookies(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    cookies = []
    for key, value in pairs:
        if key.lower() == "set-cookie":
            name, _, rest = value.partition("=")
            cookies.append({"name": name.strip(), "value": rest.split(";", 1)[0]})
    return cookies


def _is_text(mime_type: str) -> bool:
    mime_type = mime_type.split(";")[0].strip().lower()
    return bool(_TEXT_MIME.match(mime_type)) or mime_type.endswith(_TEXT_SUFFIX)


def entry_from_record(record: NetworkRecord) -> dict[str, Any]:
    started = datetime.fromtimestamp(record.started or 0, tz=timezone.utc).isoformat(timespec="milliseconds")
    wait = max(record.wait_ms, 0)
    content: dict[str, Any] = {"size": len(record.body) if record.body is not None else 0, "mimeType": record.mime_type}
    if record.body is not None:
        text = None
        if _is_text(record.mime_type):
            try:
                text = record.body.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is None:
            content["text"], content["encoding"] = base64.b64encode(record.body).decode("ascii"), "base64"
        else:
            content["text"] = text
    request: dict[str, Any] = {
        "method": record.method,
        "url": record.url,
        "httpVersion": _http_version(record.protocol),
        "cookies": _request_cookies(record.request_headers),
        "headers": [{"name": name, "value": value} for name, value in record.request_headers],
        "queryString": [
            {"name": name, "value": value}
            for name, value in parse_qsl(urlsplit(record.url).query, keep_blank_values=True)
        ],
        "headersSize": -1,
        "bodySize": len(record.post_data.encode("utf-8")) if record.post_data else 0,
    }
    if record.post_data is not None:
        request["postData"] = {"mimeType": _header(record.request_headers, "content-type"), "text": record.post_data}
    entry: dict[str, Any] = {
        "startedDateTime": started,
        "time": wait,
        "request": request,
        "response": {
            "status": record.status,
            "statusText": record.status_text,
            "httpVersion": _http_version(record.protocol),
            "cookies": _response_cookies(record.response_headers),
            "headers": [{"name": name, "value": value} for name, value in record.response_headers],
            "content": content,
            "redirectURL": _header(record.response_headers, "location"),
            "headersSize": -1,
            "bodySize": record.encoded_size or -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": wait, "receive": 0},
        "_resourceType": record.resource_type,
    }
    if record.remote_ip:
        entry["serverIPAddress"] = record.remote_ip
    if record.error:
        entry["response"]["_error"] = record.error
    return entry


def build_har(records: list[NetworkRecord], browser_version: str = "", creator_version: str = "") -> dict[str, Any]:
    log: dict[str, Any] = {
        "version": "1.2",
        "creator": {"name": "crau", "version": creator_version},
        "entries": [entry_from_record(record) for record in records],
    }
    if browser_version:
        name, _, version = browser_version.partition(" ")
        log["browser"] = {"name": name, "version": version or name}
    return {"log": log}


def value_shape(value: Any, depth: int = 0, max_depth: int = 4) -> Any:
    """Structure of a JSON value with types in place of values (first element stands for a whole list)."""
    if isinstance(value, dict):
        if depth >= max_depth:
            return "object"
        return {key: value_shape(item, depth + 1, max_depth) for key, item in value.items()}
    if isinstance(value, list):
        if depth >= max_depth:
            return "array"
        return [value_shape(value[0], depth + 1, max_depth)] if value else []
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _body_shape(text: str | None, mime_type: str) -> Any:
    if not text:
        return None
    mime_type = mime_type.split(";")[0].strip().lower()
    if mime_type == "application/x-www-form-urlencoded":
        return {name: "string" for name, _ in parse_qsl(text, keep_blank_values=True)}
    try:
        return value_shape(json.loads(text))
    except ValueError:
        return None


def summarize_har(
    har: dict[str, Any], url_filter: str | None = None, method: str | None = None, mime_filter: str | None = None
) -> list[dict[str, Any]]:
    """One small dict per matching entry: what was called and the shape of what went in and out, never the values."""
    pattern = re.compile(url_filter) if url_filter else None
    rows = []
    for index, entry in enumerate(har["log"]["entries"]):
        request, response = entry["request"], entry.get("response", {})
        content = response.get("content", {})
        mime_type = content.get("mimeType", "")
        if pattern and not pattern.search(request["url"]):
            continue
        if method and request["method"].upper() != method.upper():
            continue
        if mime_filter and mime_filter.lower() not in mime_type.lower():
            continue
        parts = urlsplit(request["url"])
        post_data = request.get("postData", {})
        text = content.get("text")
        if text and content.get("encoding") == "base64":
            text = None
        rows.append(
            {
                "entry": index,
                "method": request["method"],
                "status": response.get("status"),
                "endpoint": f"{parts.scheme}://{parts.netloc}{parts.path}",
                "query": sorted({item["name"] for item in request.get("queryString", [])}),
                "request_headers": sorted({header["name"] for header in request.get("headers", [])}),
                "request_body": _body_shape(post_data.get("text"), post_data.get("mimeType", "")),
                "mime_type": mime_type,
                "size": content.get("size", 0),
                "response_body": _body_shape(text, mime_type) if "json" in mime_type else None,
            }
        )
    return rows
