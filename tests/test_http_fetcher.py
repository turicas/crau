import asyncio
import gzip
import pytest
from crau.fetchers.http import AsyncHttpFetcher


@pytest.fixture
def run_server():
    servers = []

    async def _start(handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield _start

    for s in servers:
        s.close()


@pytest.mark.asyncio
async def test_http_fetcher_preserves_custom_headers_and_reason_phrase(run_server):
    async def handler(reader, writer):
        req_data = await reader.readuntil(b"\r\n\r\n")
        response_bytes = (
            b"HTTP/1.1 200 It Worked\r\n"
            b"X-Custom-Casing: ValueOne\r\n"
            b"x-custom-casing: ValueTwo\r\n"
            b"Content-Length: 12\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"Hello World!"
        )
        writer.write(response_bytes)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/test")

    assert len(result.transactions) == 1
    tx = result.transactions[0]
    assert tx.response.status_code == 200
    assert tx.response.reason_phrase == "It Worked"
    assert tx.response.raw_body == b"Hello World!"

    # Header preservation
    headers = tx.response.raw_headers
    assert (b"X-Custom-Casing", b"ValueOne") in headers
    assert (b"x-custom-casing", b"ValueTwo") in headers
    assert tx.duration_seconds > 0


@pytest.mark.asyncio
async def test_http_fetcher_preserves_gzip_payload_without_decompression(run_server):
    compressed_body = gzip.compress(b"Secret Compressed Payload")

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        header = f"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: {len(compressed_body)}\r\nConnection: close\r\n\r\n".encode("ascii")
        writer.write(header + compressed_body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/compressed")

    tx = result.transactions[0]
    assert (b"Content-Encoding", b"gzip") in tx.response.raw_headers
    # Body in raw_body MUST remain compressed
    assert tx.response.raw_body == compressed_body
    # But result.page.content decompresses for extractor convenience
    assert "Secret Compressed Payload" in result.page.content


@pytest.mark.asyncio
async def test_http_fetcher_handles_chunked_transfer_encoding(run_server):
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        response_bytes = (
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
            b"5\r\nHello\r\n"
            b"7\r\n World!\r\n"
            b"0\r\n\r\n"
        )
        writer.write(response_bytes)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/chunked")

    tx = result.transactions[0]
    assert (b"Transfer-Encoding", b"chunked") in tx.response.raw_headers
    assert tx.response.raw_body == b"Hello World!"
    assert result.page.content == "Hello World!"


@pytest.mark.asyncio
async def test_http_fetcher_records_redirect_chain(run_server):
    async def handler(reader, writer):
        req_line = await reader.readline()
        await reader.readuntil(b"\r\n\r\n")
        if b"/step1" in req_line:
            resp = (
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /step2\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
        else:
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n\r\n"
                b"Final"
            )
        writer.write(resp)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/step1")

    # Both redirect and final response must be recorded
    assert len(result.transactions) == 2
    assert result.transactions[0].response.status_code == 302
    assert result.transactions[1].response.status_code == 200
    assert result.page.url == f"{base_url}/step2"
    assert result.page.content == "Final"
