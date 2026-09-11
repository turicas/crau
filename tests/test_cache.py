import datetime
import io
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
