from __future__ import annotations

import base64
import datetime
import io
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


def _header_tuples_to_warc(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    """Convert raw byte header tuples to latin1 strings for warcio StatusAndHeaders."""
    return [(k.decode("latin1"), v.decode("latin1")) for k, v in raw_headers]


def _header_tuples_to_har(raw_headers: list[tuple[bytes, bytes]]) -> list[dict[str, str]]:
    """Convert raw byte header tuples to HAR headers list."""
    return [{"name": k.decode("latin1"), "value": v.decode("latin1")} for k, v in raw_headers]


def _get_content_type(raw_headers: list[tuple[bytes, bytes]]) -> str:
    for name, value in raw_headers:
        if name.lower() == b"content-type":
            return value.decode("latin1")
    return "application/octet-stream"


@dataclass
class RawRequest:
    url: str
    method: str = "GET"
    http_version: str = "HTTP/1.1"
    raw_headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    raw_body: bytes = b""
    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


@dataclass
class RawResponse:
    """HTTP response as received.

    Status line fields have no defaults on purpose: they are what the server sent and a
    fetcher must always provide them. ``raw_body`` holds the bytes exactly as they came
    over the wire (chunked framing included) and is what goes into WARC records;
    ``payload`` is the de-framed message body. When they are identical (no
    Transfer-Encoding) ``payload`` may be left None.
    """

    status_code: int
    reason_phrase: str
    http_version: str
    raw_headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    raw_body: bytes = b""
    payload: bytes | None = None
    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    @property
    def body(self) -> bytes:
        """Message body without transfer framing (what HAR content and parsers should use)."""
        return self.payload if self.payload is not None else self.raw_body


@dataclass
class NetworkTransaction:
    """One request/response pair. ``duration_seconds`` is None when unknown (e.g. replayed from a WARC)."""

    request: RawRequest
    response: RawResponse
    duration_seconds: float | None = None
    remote_address: tuple[str, int] | None = None

    def to_warc_records(self, writer: WARCWriter):
        """Generate WARC request and response records preserving raw headers and body.

        Returns (request_record, response_record); the request carries
        WARC-Concurrent-To pointing at the response, and both carry WARC-IP-Address
        when the peer address is known.
        """
        parsed = urlparse(self.request.url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        ip_headers = {}
        if self.remote_address:
            ip_headers["WARC-IP-Address"] = self.remote_address[0]

        # Response record (created first so the request can reference it)
        resp_status_line = f"{self.response.status_code} {self.response.reason_phrase}".strip()
        resp_headers = StatusAndHeaders(
            resp_status_line,
            _header_tuples_to_warc(self.response.raw_headers),
            protocol=self.response.http_version,
            is_http_request=False,
        )
        resp_warc_headers = {
            "WARC-Date": self.response.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **ip_headers,
        }
        resp_record = writer.create_warc_record(
            self.request.url,
            "response",
            payload=io.BytesIO(self.response.raw_body),
            length=len(self.response.raw_body),
            http_headers=resp_headers,
            warc_headers_dict=resp_warc_headers,
        )

        # Request record
        req_headers = StatusAndHeaders(
            f"{self.request.method} {target} {self.request.http_version}",
            _header_tuples_to_warc(self.request.raw_headers),
            protocol=self.request.http_version,
            is_http_request=True,
        )
        req_warc_headers = {
            "WARC-Date": self.request.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "WARC-Concurrent-To": resp_record.rec_headers.get_header("WARC-Record-ID"),
            **ip_headers,
        }
        req_payload = io.BytesIO(self.request.raw_body) if self.request.raw_body else None
        req_length = len(self.request.raw_body) if self.request.raw_body else 0
        req_record = writer.create_warc_record(
            self.request.url,
            "request",
            payload=req_payload,
            length=req_length,
            http_headers=req_headers,
            warc_headers_dict=req_warc_headers,
        )
        return req_record, resp_record

    def to_har_entry(self, override_text: str | None = None) -> dict:
        """Convert transaction into an entry adhering to the HAR 1.2 specification."""
        mime_type = _get_content_type(self.response.raw_headers)
        if override_text is not None:
            content_text = override_text
            content_encoding = None
            body_size = len(override_text.encode("utf-8"))
        else:
            body = self.response.body
            try:
                content_text = body.decode("utf-8")
                content_encoding = None
            except UnicodeDecodeError:
                content_text = base64.b64encode(body).decode("ascii")
                content_encoding = "base64"
            body_size = len(body)

        content_dict: dict = {
            "size": body_size,
            "mimeType": mime_type,
            "text": content_text,
        }
        if content_encoding:
            content_dict["encoding"] = content_encoding

        redirect_url = ""
        for name, value in self.response.raw_headers:
            if name.lower() == b"location":
                redirect_url = value.decode("latin1")
                break

        har_request: dict = {
            "method": self.request.method,
            "url": self.request.url,
            "httpVersion": self.request.http_version,
            "cookies": [],
            "headers": _header_tuples_to_har(self.request.raw_headers),
            "queryString": [
                {"name": name, "value": value}
                for name, value in parse_qsl(urlparse(self.request.url).query, keep_blank_values=True)
            ],
            "headersSize": -1,
            "bodySize": len(self.request.raw_body),
        }
        if self.request.raw_body:
            post_data: dict = {"mimeType": _get_content_type(self.request.raw_headers)}
            try:
                post_data["text"] = self.request.raw_body.decode("utf-8")
            except UnicodeDecodeError:
                # HAR 1.2 only defines `encoding` for response content; binary request bodies borrow it
                post_data["text"] = base64.b64encode(self.request.raw_body).decode("ascii")
                post_data["encoding"] = "base64"
            har_request["postData"] = post_data

        # HAR uses -1 for timings that are not available
        time_ms = round(self.duration_seconds * 1000, 2) if self.duration_seconds is not None else -1
        return {
            "startedDateTime": self.request.timestamp.isoformat(),
            "time": time_ms,
            "request": har_request,
            "response": {
                "status": self.response.status_code,
                "statusText": self.response.reason_phrase,
                "httpVersion": self.response.http_version,
                "cookies": [],
                "headers": _header_tuples_to_har(self.response.raw_headers),
                "content": content_dict,
                "redirectURL": redirect_url,
                "headersSize": -1,
                "bodySize": body_size,
            },
            "cache": {},
            "timings": {
                "send": 0 if time_ms >= 0 else -1,
                "wait": time_ms,
                "receive": 0 if time_ms >= 0 else -1,
            },
        }


@dataclass
class Page:
    """Rendered/decoded view of a navigation.

    ``main_transaction`` is the transaction that produced the document (the final
    hop of a redirect chain for HTTP, the navigation request for browsers); when
    None the last transaction is assumed.
    """

    url: str
    status_code: int
    content: str
    raw_body: bytes = b""
    transactions: list[NetworkTransaction] = field(default_factory=list)
    timing: dict[str, float] = field(default_factory=dict)
    main_transaction: NetworkTransaction | None = None

    def get_main_transaction(self) -> NetworkTransaction | None:
        if self.main_transaction is not None:
            return self.main_transaction
        return self.transactions[-1] if self.transactions else None


@dataclass
class CrawlResult:
    transactions: list[NetworkTransaction] = field(default_factory=list)
    page: Page | None = None


def create_har_log(
    transactions: list[NetworkTransaction],
    pages: list[Page] | None = None,
    mode: str = "raw",
    creator_name: str = "crau",
    creator_version: str = "1.0.0",
) -> dict:
    """Format network transactions into a full HAR 1.2 root object.

    Modes:
      - 'raw': all captured network transactions with original wire bodies.
      - 'rendered': all captured network transactions, but target pages have their
        response text replaced by the rendered DOM HTML.
      - 'rendered-only': only the target navigation pages, with rendered DOM HTML.
    """
    entries = []
    pages_list = pages or []

    # Map main transaction IDs to rendered DOM content
    rendered_by_tx_id: dict[int, str] = {}
    main_transactions: list[tuple[NetworkTransaction, str]] = []
    for page in pages_list:
        main_tx = page.get_main_transaction()
        if main_tx is not None:
            rendered_by_tx_id[id(main_tx)] = page.content
            main_transactions.append((main_tx, page.content))

    if mode == "rendered-only":
        for tx, rendered_text in main_transactions:
            entries.append(tx.to_har_entry(override_text=rendered_text))
    elif mode == "rendered":
        for tx in transactions:
            override = rendered_by_tx_id.get(id(tx))
            entries.append(tx.to_har_entry(override_text=override))
    else:  # 'raw'
        for tx in transactions:
            entries.append(tx.to_har_entry())

    return {
        "log": {
            "version": "1.2",
            "creator": {
                "name": creator_name,
                "version": creator_version,
            },
            "entries": entries,
        }
    }


def create_warcinfo_record(writer: WARCWriter, filename: str, creator_version: str = "1.0.0"):
    """warcinfo record identifying the software that produced the file."""
    return writer.create_warcinfo_record(
        filename,
        {
            "software": f"crau/{creator_version} https://github.com/turicas/crau",
            "format": "WARC File Format 1.0",
        },
    )
