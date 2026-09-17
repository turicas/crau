from __future__ import annotations

import datetime
import io
from pathlib import Path
from typing import Any

from warcio.archiveiterator import ArchiveIterator
from warcio.bufferedreaders import ChunkedDataReader

from crau.cache.base import CacheBackend
from crau.models import NetworkTransaction, RawRequest, RawResponse


def _parse_request_line(http_headers) -> tuple[str, str]:
    """Method and HTTP version of a request record.

    warcio splits "GET /path HTTP/1.1" as protocol="GET" and statusline="/path HTTP/1.1",
    so the method is in `protocol` and the version is the last token of `statusline`.
    """
    method, http_version = "GET", "HTTP/1.1"
    if http_headers is None:
        return method, http_version
    if http_headers.protocol:
        method = http_headers.protocol
    parts = (http_headers.statusline or "").rsplit(" ", 1)
    if len(parts) == 2 and parts[1].upper().startswith("HTTP/"):
        http_version = parts[1]
    return method, http_version


class WarcCacheBackend(CacheBackend):
    """Cache backend that serves requests from pre-existing WARC archives."""

    def __init__(self, warc_paths: str | Path | list[str | Path]):
        if isinstance(warc_paths, (str, Path)):
            self.warc_paths = [Path(warc_paths)]
        else:
            self.warc_paths = [Path(p) for p in warc_paths]

        self._cache: dict[str, NetworkTransaction] = {}
        self._loaded = False

    def _load_warcs(self) -> None:
        if self._loaded:
            return

        for warc_file in self.warc_paths:
            if not warc_file.exists():
                continue

            with open(warc_file, "rb") as fobj:
                last_req: dict[str, Any] = {}

                for record in ArchiveIterator(fobj):
                    target_uri = record.rec_headers.get_header("WARC-Target-URI")
                    if not target_uri:
                        continue

                    warc_date_str = record.rec_headers.get_header("WARC-Date")
                    try:
                        record_date = datetime.datetime.fromisoformat(warc_date_str)
                    except Exception:
                        record_date = datetime.datetime.now(datetime.timezone.utc)

                    if record.rec_type == "request":
                        http_h = record.http_headers
                        raw_headers = [
                            (k.encode("latin1"), v.encode("latin1"))
                            for k, v in (http_h.headers if http_h else [])
                        ]
                        body = record.content_stream().read()
                        method, http_version = _parse_request_line(http_h)
                        last_req[target_uri] = RawRequest(
                            url=target_uri,
                            method=method,
                            http_version=http_version,
                            raw_headers=raw_headers,
                            raw_body=body,
                            timestamp=record_date,
                        )

                    elif record.rec_type == "response":
                        http_h = record.http_headers
                        raw_headers = [
                            (k.encode("latin1"), v.encode("latin1"))
                            for k, v in (http_h.headers if http_h else [])
                        ]
                        # Keep wire bytes (may still be chunked) and derive the de-framed payload
                        raw_body = record.raw_stream.read()
                        payload = raw_body
                        transfer_encoding = (http_h.get_header("Transfer-Encoding") if http_h else "") or ""
                        if "chunked" in transfer_encoding.lower():
                            payload = ChunkedDataReader(io.BytesIO(raw_body)).read()
                        body = payload
                        status_code = 200
                        reason_phrase = "OK"
                        if http_h and http_h.statusline:
                            parts = http_h.statusline.split(" ", 1)
                            if parts[0].isdigit():
                                status_code = int(parts[0])
                            if len(parts) > 1:
                                reason_phrase = parts[1]

                        req = last_req.get(
                            target_uri,
                            RawRequest(url=target_uri, method="GET", timestamp=record_date),
                        )
                        resp = RawResponse(
                            status_code=status_code,
                            reason_phrase=reason_phrase,
                            http_version=(http_h.protocol if http_h and http_h.protocol else "HTTP/1.1"),
                            raw_headers=raw_headers,
                            raw_body=raw_body,
                            payload=body,
                            timestamp=record_date,
                        )
                        self._cache[target_uri] = NetworkTransaction(
                            request=req,
                            response=resp,
                            duration_seconds=None,
                        )

        self._loaded = True

    async def __aenter__(self) -> "WarcCacheBackend":
        self._load_warcs()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self._cache.clear()

    async def get(self, url: str) -> list[NetworkTransaction] | None:
        self._load_warcs()
        tx = self._cache.get(url)
        return [tx] if tx is not None else None

    async def store(self, url: str, transactions: list[NetworkTransaction]) -> None:
        # Memory-only update for session duration
        if transactions:
            self._cache[url] = transactions[-1]
