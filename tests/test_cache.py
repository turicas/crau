import time
import pytest
from warcio.warcwriter import WARCWriter

from crau.cache.sqlite import SqliteCacheBackend
from crau.cache.warc import WarcCacheBackend
from crau.models import NetworkTransaction, RawRequest, RawResponse


def _make_dummy_tx(url: str, status_code: int = 200, headers: list | None = None) -> NetworkTransaction:
    headers = headers or [(b"Content-Type", b"text/html")]
    req = RawRequest(url=url, method="GET")
    resp = RawResponse(
        status_code=status_code,
        reason_phrase="OK",
        http_version="HTTP/1.1",
        raw_headers=headers,
        raw_body=b"Cached content",
    )
    return NetworkTransaction(request=req, response=resp, duration_seconds=0.01)


@pytest.mark.asyncio
async def test_sqlite_cache_store_and_get():
    async with SqliteCacheBackend(":memory:") as cache:
        tx = _make_dummy_tx("https://example.com/item")
        await cache.store("https://example.com/item", [tx])

        cached = await cache.get("https://example.com/item")
        assert cached is not None
        assert len(cached) == 1
        assert cached[0].request.url == "https://example.com/item"
        assert cached[0].response.raw_body == b"Cached content"


@pytest.mark.asyncio
async def test_sqlite_cache_respects_no_store():
    async with SqliteCacheBackend(":memory:") as cache:
        tx = _make_dummy_tx(
            "https://example.com/private",
            headers=[(b"Cache-Control", b"no-store")],
        )
        await cache.store("https://example.com/private", [tx])
        cached = await cache.get("https://example.com/private")
        assert cached is None


@pytest.mark.asyncio
async def test_sqlite_cache_expiration():
    async with SqliteCacheBackend(":memory:") as cache:
        tx = _make_dummy_tx(
            "https://example.com/expiring",
            headers=[(b"Cache-Control", b"max-age=0")],
        )
        await cache.store("https://example.com/expiring", [tx])
        time.sleep(0.01)
        cached = await cache.get("https://example.com/expiring")
        assert cached is None


@pytest.mark.asyncio
async def test_warc_cache_backend(tmp_path):
    warc_file = tmp_path / "cache.warc.gz"
    with open(warc_file, "wb") as fobj:
        writer = WARCWriter(fobj, gzip=True)
        tx = _make_dummy_tx("https://example.com/archived")
        r1, r2 = tx.to_warc_records(writer)
        writer.write_record(r1)
        writer.write_record(r2)

    async with WarcCacheBackend(warc_file) as cache:
        cached = await cache.get("https://example.com/archived")
        assert cached is not None
        assert cached[0].request.url == "https://example.com/archived"
        assert cached[0].response.raw_body == b"Cached content"

        miss = await cache.get("https://example.com/not-there")
        assert miss is None


def test_warc_cache_backend_deframes_chunked_records(tmp_path):
    import asyncio
    import io

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    from crau.cache.warc import WarcCacheBackend

    wire = b"3\r\nabc\r\n0\r\n\r\n"
    warc_path = tmp_path / "chunked.warc"
    with open(warc_path, "wb") as fobj:
        writer = WARCWriter(fobj, gzip=False)
        headers = StatusAndHeaders("200 OK", [("Transfer-Encoding", "chunked")], protocol="HTTP/1.1")
        writer.write_record(
            writer.create_warc_record(
                "http://example.com/c", "response", payload=io.BytesIO(wire), length=len(wire), http_headers=headers
            )
        )

    async def run():
        async with WarcCacheBackend(warc_path) as cache:
            return await cache.get("http://example.com/c")

    transactions = asyncio.run(run())
    response = transactions[0].response
    assert response.raw_body == wire
    assert response.body == b"abc"


@pytest.mark.asyncio
async def test_sqlite_cache_raises_when_not_opened():
    cache = SqliteCacheBackend(":memory:")
    with pytest.raises(RuntimeError):
        await cache.get("http://example.com/")


@pytest.mark.asyncio
async def test_sqlite_cache_entering_twice_keeps_connection():
    async with SqliteCacheBackend(":memory:") as cache:
        conn = cache._conn
        await cache.__aenter__()
        assert cache._conn is conn


@pytest.mark.asyncio
async def test_warc_cache_backend_restores_method_and_http_versions(tmp_path):
    import io

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    from crau.cache.warc import WarcCacheBackend

    warc_path = tmp_path / "versions.warc"
    with open(warc_path, "wb") as fobj:
        writer = WARCWriter(fobj, gzip=False)
        req_headers = StatusAndHeaders("POST /form HTTP/1.0", [("Host", "example.com")], is_http_request=True)
        writer.write_record(
            writer.create_warc_record(
                "http://example.com/form", "request", payload=io.BytesIO(b"a=1"), length=3, http_headers=req_headers
            )
        )
        resp_headers = StatusAndHeaders("201 Created", [("Content-Length", "2")], protocol="HTTP/1.0")
        writer.write_record(
            writer.create_warc_record(
                "http://example.com/form", "response", payload=io.BytesIO(b"ok"), length=2, http_headers=resp_headers
            )
        )

    async with WarcCacheBackend(warc_path) as cache:
        (tx,) = await cache.get("http://example.com/form")

    assert tx.request.method == "POST"
    assert tx.request.http_version == "HTTP/1.0"
    assert tx.request.raw_body == b"a=1"
    assert tx.response.status_code == 201
    assert tx.response.reason_phrase == "Created"
    assert tx.response.http_version == "HTTP/1.0"


@pytest.mark.asyncio
async def test_sqlite_cache_round_trips_unknown_duration():
    tx = _make_dummy_tx("http://example.com/")
    tx.duration_seconds = None
    async with SqliteCacheBackend(":memory:") as cache:
        await cache.store("http://example.com/", [tx])
        (cached,) = await cache.get("http://example.com/")
    assert cached.duration_seconds is None
