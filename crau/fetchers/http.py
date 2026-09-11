from __future__ import annotations

import asyncio
import datetime
import gzip
import ssl
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import h11

from crau.fetchers.base import Fetcher
from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse
from crau.version import __version__


def _parse_raw_header_block(
    header_block: bytes,
) -> tuple[str, int, str, list[tuple[bytes, bytes]]]:
    """Parse raw bytes before body into http_version, status_code, reason and byte headers."""
    lines = header_block.split(b"\r\n")
    first_line = lines[0].decode("latin1", errors="replace")
    parts = first_line.split(" ", 2)
    http_version = parts[0] if len(parts) > 0 else "HTTP/1.1"
    status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 200
    reason = parts[2] if len(parts) > 2 else ""

    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        if not line or b":" not in line:
            continue
        key, val = line.split(b":", 1)
        headers.append((key, val.strip(b" \t")))

    return http_version, status_code, reason, headers
    for name, value in headers:
        if name.lower() == b"content-encoding":
            encoding = value.strip().lower()
            if encoding == b"gzip":
                try:
                    return gzip.decompress(body)
                except Exception:
                    pass
    return body


def _decompress_if_needed(body: bytes, headers: list[tuple[bytes, bytes]]) -> bytes:
    for name, value in headers:
        if name.lower() == b"content-encoding":
            encoding = value.strip().lower()
            if encoding == b"gzip":
                try:
                    return gzip.decompress(body)
                except Exception:
                    pass
    return body


def _decode_content(body: bytes, headers: list[tuple[bytes, bytes]]) -> str:
    # Try decompression if response was encoded (only for text representation)
    decompressed = _decompress_if_needed(body, headers)
    for encoding in ("utf-8", "latin-1", "ascii"):
        try:
            return decompressed.decode(encoding)
        except UnicodeDecodeError:
            continue
    return decompressed.decode("latin-1", errors="replace")


class AsyncHttpFetcher(Fetcher):
    """High-fidelity HTTP/1.1 fetcher using h11 and raw TCP sockets."""

    def __init__(
        self,
        user_agent: str | None = None,
        timeout: float = 15.0,
        redirect_limit: int = 20,
    ):
        self.user_agent = user_agent or f"crau/{__version__}"
        self.timeout = timeout
        self.redirect_limit = redirect_limit

    async def __aenter__(self) -> "AsyncHttpFetcher":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    async def _fetch_single(
        self,
        url: str,
        method: str = "GET",
        headers: list[tuple[bytes, bytes]] | None = None,
        body: bytes = b"",
    ) -> NetworkTransaction:
        parsed = urlparse(url)
        is_ssl = parsed.scheme.lower() == "https"
        port = parsed.port or (443 if is_ssl else 80)
        host = parsed.hostname or ""

        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        # Construct request headers
        req_headers_list: list[tuple[bytes, bytes]] = []
        host_header = host.encode("idna")
        if (is_ssl and port != 443) or (not is_ssl and port != 80):
            host_header = f"{host}:{port}".encode("idna")

        req_headers_list.append((b"Host", host_header))
        req_headers_list.append((b"User-Agent", self.user_agent.encode("latin1")))
        req_headers_list.append((b"Accept", b"*/*"))
        req_headers_list.append((b"Connection", b"close"))
        if headers:
            req_headers_list.extend(headers)

        ssl_ctx = ssl.create_default_context() if is_ssl else None

        req_timestamp = datetime.datetime.now(datetime.timezone.utc)
        start_time = time.perf_counter()

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx),
            timeout=self.timeout,
        )

        h11_conn = h11.Connection(our_role=h11.CLIENT)
        req_event = h11.Request(
            method=method.encode("ascii"),
            target=target.encode("ascii"),
            headers=req_headers_list,
        )
        writer.write(h11_conn.send(req_event))
        if body:
            writer.write(h11_conn.send(h11.Data(data=body)))
        writer.write(h11_conn.send(h11.EndOfMessage()))
        await writer.drain()

        resp_status_code = 0
        resp_reason = ""
        resp_http_version = "HTTP/1.1"
        resp_headers: list[tuple[bytes, bytes]] = []
        resp_chunks: list[bytes] = []
        raw_header_buffer = bytearray()
        headers_parsed = False

        try:
            while True:
                event = h11_conn.next_event()
                if event is h11.NEED_DATA:
                    data = await asyncio.wait_for(
                        reader.read(8192),
                        timeout=self.timeout,
                    )
                    if not headers_parsed and data:
                        raw_header_buffer.extend(data)
                        if b"\r\n\r\n" in raw_header_buffer:
                            idx = raw_header_buffer.find(b"\r\n\r\n")
                            header_block = bytes(raw_header_buffer[:idx])
                            (
                                resp_http_version,
                                resp_status_code,
                                resp_reason,
                                resp_headers,
                            ) = _parse_raw_header_block(header_block)
                            headers_parsed = True

                    if not data:
                        h11_conn.receive_data(b"")
                    else:
                        h11_conn.receive_data(data)
                    continue

                if isinstance(event, h11.Response):
                    if not headers_parsed:
                        resp_status_code = event.status_code
                        resp_reason = event.reason.decode("latin1")
                        resp_http_version = f"HTTP/{event.http_version.decode('ascii')}"
                        resp_headers = list(event.headers)
                elif isinstance(event, h11.Data):
                    resp_chunks.append(event.data)
                elif isinstance(event, (h11.EndOfMessage, h11.PAUSED)):
                    break
                elif isinstance(event, h11.ConnectionClosed):
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        end_time = time.perf_counter()
        resp_timestamp = datetime.datetime.now(datetime.timezone.utc)

        raw_request = RawRequest(
            url=url,
            method=method,
            http_version="HTTP/1.1",
            raw_headers=req_headers_list,
            raw_body=body,
            timestamp=req_timestamp,
        )

        raw_response = RawResponse(
            status_code=resp_status_code,
            reason_phrase=resp_reason,
            http_version=resp_http_version,
            raw_headers=resp_headers,
            raw_body=b"".join(resp_chunks),
            timestamp=resp_timestamp,
        )

        return NetworkTransaction(
            request=raw_request,
            response=raw_response,
            duration_seconds=end_time - start_time,
        )

    async def fetch(self, url: str, **kwargs: Any) -> CrawlResult:
        current_url = url
        transactions: list[NetworkTransaction] = []

        for _ in range(self.redirect_limit + 1):
            tx = await self._fetch_single(current_url)
            transactions.append(tx)

            if tx.response.status_code in (301, 302, 303, 307, 308):
                location = None
                for k, v in tx.response.raw_headers:
                    if k.lower() == b"location":
                        location = v.decode("latin1")
                        break
                if location:
                    current_url = urljoin(current_url, location)
                    continue
            break

        final_tx = transactions[-1]
        decoded_content = _decode_content(
            final_tx.response.raw_body, final_tx.response.raw_headers
        )

        page = Page(
            url=current_url,
            status_code=final_tx.response.status_code,
            content=decoded_content,
            raw_body=final_tx.response.raw_body,
            transactions=transactions,
            timing={"total": sum(t.duration_seconds for t in transactions)},
        )

        return CrawlResult(transactions=transactions, page=page)
