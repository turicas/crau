import datetime
import io
import json
from warcio.archiveiterator import ArchiveIterator
from warcio.warcwriter import WARCWriter

from crau.models import (
    CrawlResult,
    NetworkTransaction,
    Page,
    RawRequest,
    RawResponse,
    create_har_log,
)


def test_raw_request_and_response_creation():
    now = datetime.datetime.now(datetime.timezone.utc)
    req = RawRequest(
        url="https://example.com/test",
        method="GET",
        raw_headers=[(b"Host", b"example.com"), (b"X-Custom-Header", b"CustomValue123")],
        raw_body=b"",
        timestamp=now,
    )
    resp = RawResponse(
        status_code=200,
        reason_phrase="OK",
        http_version="HTTP/1.1",
        raw_headers=[(b"Content-Type", b"text/html; charset=utf-8"), (b"Server", b"CustomServer/1.0")],
        raw_body=b"<html>Hello</html>",
        timestamp=now,
    )
    tx = NetworkTransaction(request=req, response=resp, duration_seconds=0.123)

    assert tx.request.url == "https://example.com/test"
    assert tx.response.status_code == 200
    assert tx.response.reason_phrase == "OK"
    assert tx.duration_seconds == 0.123


def test_network_transaction_to_warc_records():
    now = datetime.datetime(2026, 4, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
    req = RawRequest(
        url="https://example.com/data",
        method="POST",
        raw_headers=[(b"Host", b"example.com"), (b"Content-Type", b"application/json")],
        raw_body=b'{"key": "value"}',
        timestamp=now,
    )
    resp = RawResponse(
        status_code=201,
        reason_phrase="Created",
        http_version="HTTP/1.1",
        raw_headers=[(b"Content-Type", b"application/json"), (b"X-Foo", b"Bar")],
        raw_body=b'{"created": true}',
        timestamp=now,
    )
    tx = NetworkTransaction(request=req, response=resp, duration_seconds=0.05)

    buffer = io.BytesIO()
    writer = WARCWriter(buffer, gzip=False)
    req_record, resp_record = tx.to_warc_records(writer)
    writer.write_record(req_record)
    writer.write_record(resp_record)

    buffer.seek(0)
    it = iter(ArchiveIterator(buffer))
    rec1 = next(it)
    assert rec1.rec_type == "request"
    assert rec1.rec_headers.get_header("WARC-Target-URI") == "https://example.com/data"
    req_http_headers = rec1.http_headers
    assert req_http_headers.protocol == "POST"
    assert "/data HTTP/1.1" in str(req_http_headers.statusline)
    assert req_http_headers.get_header("Content-Type") == "application/json"
    assert rec1.content_stream().read() == b'{"key": "value"}'

    rec2 = next(it)
    assert rec2.rec_type == "response"
    assert rec2.rec_headers.get_header("WARC-Target-URI") == "https://example.com/data"
    resp_http_headers = rec2.http_headers
    assert resp_http_headers.protocol == "HTTP/1.1"
    assert resp_http_headers.statusline == "201 Created"
    assert resp_http_headers.get_header("X-Foo") == "Bar"
    assert rec2.content_stream().read() == b'{"created": true}'


def test_network_transaction_to_har_entry():
    now = datetime.datetime(2026, 4, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
    req = RawRequest(
        url="https://example.com/page",
        method="GET",
        raw_headers=[(b"User-Agent", b"crau-test")],
        raw_body=b"",
        timestamp=now,
    )
    resp = RawResponse(
        status_code=200,
        reason_phrase="OK",
        http_version="HTTP/1.1",
        raw_headers=[(b"Content-Type", b"text/html")],
        raw_body=b"<h1>Test</h1>",
        timestamp=now,
    )
    tx = NetworkTransaction(request=req, response=resp, duration_seconds=0.25)

    entry = tx.to_har_entry()
    assert entry["startedDateTime"] == "2026-04-18T12:00:00+00:00"
    assert entry["time"] == 250.0  # in milliseconds
    assert entry["request"]["method"] == "GET"
    assert entry["request"]["url"] == "https://example.com/page"
    assert entry["request"]["headers"] == [{"name": "User-Agent", "value": "crau-test"}]
    assert entry["response"]["status"] == 200
    assert entry["response"]["statusText"] == "OK"
    assert entry["response"]["content"]["text"] == "<h1>Test</h1>"


def test_create_har_log():
    tx = NetworkTransaction(
        request=RawRequest(url="https://example.com/a"),
        response=RawResponse(status_code=200, reason_phrase="OK", raw_body=b"OK"),
        duration_seconds=0.1,
    )
    har = create_har_log([tx], creator_version="1.0.0")
    assert "log" in har
    assert har["log"]["version"] == "1.2"
    assert har["log"]["creator"]["name"] == "crau"
    assert len(har["log"]["entries"]) == 1


def test_page_and_crawl_result():
    tx = NetworkTransaction(
        request=RawRequest(url="https://example.com/home"),
        response=RawResponse(status_code=200, reason_phrase="OK", raw_body=b"<html>DOM</html>"),
    )
    page = Page(
        url="https://example.com/home",
        status_code=200,
        content="<html>DOM</html>",
        raw_body=b"<html>DOM</html>",
        transactions=[tx],
    )
    res = CrawlResult(transactions=[tx], page=page)
    assert res.page.url == "https://example.com/home"
    assert len(res.transactions) == 1
