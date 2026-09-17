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
        await reader.readuntil(b"\r\n\r\n")
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
    assert tx.response.body == b"Hello World!"
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


@pytest.mark.asyncio
async def test_http_fetcher_quotes_non_ascii_url_before_sending(run_server):
    seen = {}

    async def handler(reader, writer):
        req_line = await reader.readline()
        await reader.readuntil(b"\r\n\r\n")
        seen["request_line"] = req_line
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/página?q=ação")

    assert seen["request_line"] == b"GET /p%C3%A1gina?q=a%C3%A7%C3%A3o HTTP/1.1\r\n"
    assert result.page.status_code == 200
    assert result.transactions[0].request.url.endswith("/p%C3%A1gina?q=a%C3%A7%C3%A3o")


@pytest.mark.asyncio
async def test_http_fetcher_decodes_page_text_with_declared_charset(run_server):
    body = "<html><body>Informação</body></html>".encode("iso-8859-1")

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=iso-8859-1\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/")

    assert "Informação" in result.page.content
    assert result.page.raw_body == body


@pytest.mark.asyncio
async def test_http_fetcher_timeout_is_total_not_per_read(run_server):
    async def drip_handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n")
        await writer.drain()
        try:
            for _ in range(100):
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    base_url = await run_server(drip_handler)
    started = asyncio.get_running_loop().time()
    async with AsyncHttpFetcher(timeout=0.5) as fetcher:
        with pytest.raises(asyncio.TimeoutError):
            await fetcher.fetch(f"{base_url}/slow")
    elapsed = asyncio.get_running_loop().time() - started
    # each read arrives well within 0.5s, so a per-read timeout would never fire
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_http_fetcher_records_remote_address(run_server):
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    port = int(base_url.rsplit(":", 1)[1])
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/")
    assert result.transactions[0].remote_address == ("127.0.0.1", port)


def test_http_fetcher_reuses_ssl_context():
    from crau.fetchers import http as http_module

    http_module._SSL_CONTEXT = None
    first = http_module._get_ssl_context()
    assert http_module._get_ssl_context() is first


@pytest.mark.asyncio
async def test_http_fetcher_keeps_chunked_wire_bytes_and_deframed_payload(run_server):
    wire_body = b"7\r\nchunk-0\r\n7\r\nchunk-1\r\n0\r\n\r\n"

    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            + wire_body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/chunked")

    response = result.transactions[0].response
    assert response.raw_body == wire_body
    assert response.payload == b"chunk-0chunk-1"
    assert response.body == b"chunk-0chunk-1"
    assert result.page.raw_body == b"chunk-0chunk-1"
    assert result.page.content == "chunk-0chunk-1"


@pytest.mark.asyncio
async def test_http_fetcher_content_length_body_has_identical_wire_and_payload(run_server):
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    base_url = await run_server(handler)
    async with AsyncHttpFetcher() as fetcher:
        result = await fetcher.fetch(f"{base_url}/")
    response = result.transactions[0].response
    assert response.raw_body == response.payload == b"hello"
