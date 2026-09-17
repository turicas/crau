import asyncio
import json
from dataclasses import dataclass
import pytest
from warcio.archiveiterator import ArchiveIterator

from crau.crawler import Crawler, Request, Response, Spider
from crau.pipeline import ItemPipeline


@pytest.fixture
def run_test_site():
    servers = []

    async def _start():
        async def handler(reader, writer):
            req_line = await reader.readline()
            await reader.readuntil(b"\r\n\r\n")

            if b"/page1" in req_line:
                html = """
                <html>
                <body>
                    <h1>Page 1</h1>
                    <link rel="stylesheet" href="/css/site.css">
                    <img src="/img/pic.png">
                    <a href="/page2">Go to Page 2</a>
                </body>
                </html>
                """
                body = html.encode("utf-8")
                header = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                resp = header + body
            elif b"/page2" in req_line:
                html = """
                <html>
                <body>
                    <h1>Page 2</h1>
                </body>
                </html>
                """
                body = html.encode("utf-8")
                header = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                resp = header + body
            elif b"/css/site.css" in req_line:
                body = b"@font-face { src: url('/fonts/site.woff2'); } body { background: url(../img/bg.png); }"
                header = f"HTTP/1.1 200 OK\r\nContent-Type: text/css\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                resp = header + body
            elif b"/fonts/site.woff2" in req_line or b"/img/bg.png" in req_line:
                body = b"BINARY"
                header = f"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                resp = header + body
            elif b"/img/pic.png" in req_line:
                body = b"FAKEDATA"
                header = f"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                resp = header + body
            else:
                resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

            writer.write(resp)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield _start

    for s in servers:
        s.close()


@pytest.mark.asyncio
async def test_crawler_default_archiving_to_warc(run_test_site, tmp_path):
    base_url = await run_test_site()
    warc_file = tmp_path / "output.warc.gz"

    crawler = Crawler(
        start_urls=[f"{base_url}/page1"],
        max_depth=1,
        warc_filename=warc_file,
    )
    await crawler.crawl()

    assert warc_file.exists()
    records = []
    with open(warc_file, "rb") as fobj:
        rec_types = []
        for r in ArchiveIterator(fobj):
            rec_types.append(r.rec_type)
            if r.rec_type == "response":
                records.append(r.rec_headers.get_header("WARC-Target-URI"))

    assert f"{base_url}/page1" in records
    assert f"{base_url}/img/pic.png" in records
    assert f"{base_url}/page2" in records
    # url() references inside the external stylesheet are archived as dependencies
    assert f"{base_url}/css/site.css" in records
    assert f"{base_url}/fonts/site.woff2" in records
    assert f"{base_url}/img/bg.png" in records
    assert rec_types[0] == "warcinfo"
    assert rec_types.count("warcinfo") == 1


@pytest.mark.asyncio
async def test_crawler_archiving_to_har(run_test_site, tmp_path):
    base_url = await run_test_site()
    har_file = tmp_path / "output.har"

    crawler = Crawler(
        start_urls=[f"{base_url}/page1"],
        max_depth=0,
        har_filename=har_file,
    )
    await crawler.crawl()

    assert har_file.exists()
    import json

    data = json.loads(har_file.read_text())
    assert "log" in data
    assert len(data["log"]["entries"]) >= 2  # page1 and img/pic.png


@dataclass
class TituloItem:
    titulo: str
    url: str


@pytest.mark.asyncio
async def test_crawler_functional_api_with_dataclass(run_test_site, tmp_path):
    base_url = await run_test_site()
    pipeline = ItemPipeline(output_dir=tmp_path, default_format="csv")

    crawler = Crawler(
        start_urls=[f"{base_url}/page1"],
        pipeline=pipeline,
    )

    @crawler.on_response
    async def parse(response: Response):
        h1 = response.xpath("//h1/text()")
        title = h1[0] if h1 else ""
        yield TituloItem(titulo=title, url=response.url)
        for link in response.xpath("//a/@href"):
            yield Request(response.urljoin(link))

    await crawler.crawl()

    csv_file = tmp_path / "TituloItem.csv"
    assert csv_file.exists()
    import csv

    with open(csv_file, newline="") as f:
        rows = list(csv.reader(f))
        assert rows[0] == ["titulo", "url"]
        assert len(rows) == 3  # Header + page1 + page2


@pytest.mark.asyncio
async def test_crawler_spider_class_api(run_test_site, tmp_path):
    base_url = await run_test_site()
    pipeline = ItemPipeline(output_dir=tmp_path)

    class CustomSpider(Spider):
        start_urls = [f"{base_url}/page1"]

        async def parse(self, response: Response):
            for link in response.xpath("//a/@href"):
                yield Request(response.urljoin(link), callback=self.parse_page2)

        async def parse_page2(self, response: Response):
            h1 = response.xpath("//h1/text()")
            yield {"page2_title": h1[0] if h1 else ""}

    spider = CustomSpider()
    crawler = Crawler(
        start_urls=spider.start_urls,
        pipeline=pipeline,
    )
    crawler.on_response(spider.parse)
    await crawler.crawl()

    jsonl_file = tmp_path / "items.jsonl"
    assert jsonl_file.exists()
    import json
    data = [json.loads(line) for line in jsonl_file.read_text().splitlines()]
    assert len(data) == 1
    assert data[0]["page2_title"] == "Page 2"


@pytest.mark.asyncio
async def test_crawler_rendered_har_format(run_test_site, tmp_path):
    import json
    base_url = await run_test_site()
    har_file = tmp_path / "rendered.har"

    crawler = Crawler(
        start_urls=[f"{base_url}/page1"],
        max_depth=0,
        har_filename=har_file,
        format="rendered-har",
    )
    await crawler.crawl()

    assert har_file.exists()
    data = json.loads(har_file.read_text())
    entries = data["log"]["entries"]
    # Contains both page1 and the dependency img/pic.png
    urls = [e["request"]["url"] for e in entries]
    assert f"{base_url}/page1" in urls
    assert f"{base_url}/img/pic.png" in urls

    page1_entry = [e for e in entries if e["request"]["url"] == f"{base_url}/page1"][0]
    assert "<h1>Page 1</h1>" in page1_entry["response"]["content"]["text"]


@pytest.mark.asyncio
async def test_crawler_rendered_only_har_format(run_test_site, tmp_path):
    import json
    base_url = await run_test_site()
    har_file = tmp_path / "rendered_only.har"

    crawler = Crawler(
        start_urls=[f"{base_url}/page1"],
        max_depth=1,
        har_filename=har_file,
        format="rendered-only-har",
    )
    await crawler.crawl()

    assert har_file.exists()
    data = json.loads(har_file.read_text())
    entries = data["log"]["entries"]
    # Only target navigation pages (page1 and page2), NO img/pic.png dependency!
    urls = [e["request"]["url"] for e in entries]
    assert f"{base_url}/page1" in urls
    assert f"{base_url}/page2" in urls
    assert f"{base_url}/img/pic.png" not in urls
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_crawler_css_dependencies_ignore_depth_and_allowed_uris(run_test_site, tmp_path):
    """Assets referenced by a stylesheet are needed at depth 0 even when anchors are not followed."""
    base_url = await run_test_site()
    warc_file = tmp_path / "output.warc.gz"
    crawler = Crawler(
        start_urls=[f"{base_url}/page1"],
        max_depth=0,
        allowed_uris=["nothing.invalid"],
        warc_filename=warc_file,
        cache_type="none",
    )
    await crawler.crawl()

    with open(warc_file, "rb") as fobj:
        uris = {r.rec_headers.get_header("WARC-Target-URI") for r in ArchiveIterator(fobj) if r.rec_type == "response"}
    assert f"{base_url}/fonts/site.woff2" in uris
    assert f"{base_url}/img/bg.png" in uris
    assert f"{base_url}/page2" not in uris


@pytest.fixture
def run_slow_site():
    """Site where every response takes `delay` seconds; used to measure real concurrency."""
    servers = []

    async def _start(delay: float = 0.3, links: int = 8):
        async def handler(reader, writer):
            req_line = await reader.readline()
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(delay)
            if b"GET /index" in req_line:
                anchors = "".join(f'<a href="/page/{i}">{i}</a>' for i in range(links))
                body = f"<html><body>{anchors}</body></html>".encode()
                content_type = b"text/html"
            elif b"GET /fail" in req_line:
                writer.close()
                return
            else:
                body = b"<html><body><h1>page</h1></body></html>"
                content_type = b"text/html"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type + b"\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield _start

    for server in servers:
        server.close()


@pytest.mark.asyncio
async def test_crawler_fetches_concurrently(run_slow_site, tmp_path):
    delay, links = 0.3, 8
    base_url = await run_slow_site(delay=delay, links=links)
    crawler = Crawler(
        start_urls=[f"{base_url}/index"],
        max_depth=1,
        allowed_uris=[base_url],
        warc_filename=tmp_path / "out.warc",
        cache_type="none",
        autothrottle=False,
        concurrency=links,
        concurrency_per_domain=links,
    )
    started = asyncio.get_running_loop().time()
    await crawler.crawl()
    elapsed = asyncio.get_running_loop().time() - started

    assert crawler.stats["fetched"] == links + 1
    # index (1 round) + all pages in parallel (1 round); sequential would take (links + 1) * delay
    assert elapsed < (links + 1) * delay * 0.6, f"crawl took {elapsed:.2f}s: fetches are not concurrent"


@pytest.mark.asyncio
async def test_crawler_respects_per_domain_limit(run_slow_site, tmp_path):
    delay, links = 0.2, 4
    base_url = await run_slow_site(delay=delay, links=links)
    crawler = Crawler(
        start_urls=[f"{base_url}/index"],
        max_depth=1,
        allowed_uris=[base_url],
        cache_type="none",
        autothrottle=False,
        concurrency=16,
        concurrency_per_domain=1,
    )
    started = asyncio.get_running_loop().time()
    await crawler.crawl()
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= (links + 1) * delay * 0.9


@pytest.mark.asyncio
async def test_crawler_logs_and_counts_failed_urls(run_slow_site, caplog):
    base_url = await run_slow_site(delay=0)
    crawler = Crawler(
        start_urls=[f"{base_url}/fail", f"{base_url}/ok"],
        max_depth=0,
        cache_type="none",
        autothrottle=False,
        max_retries=1,
    )
    with caplog.at_level("INFO", logger="crau"):
        await crawler.crawl()

    assert crawler.stats["failed"] == 1
    assert crawler.stats["fetched"] == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("Retrying" in message and "/fail" in message for message in messages)
    assert any("Giving up" in message and "/fail" in message for message in messages)


@pytest.mark.asyncio
async def test_crawler_reports_progress(run_slow_site):
    base_url = await run_slow_site(delay=0, links=2)
    snapshots = []
    crawler = Crawler(
        start_urls=[f"{base_url}/index"],
        max_depth=1,
        allowed_uris=[base_url],
        cache_type="none",
        autothrottle=False,
        progress=snapshots.append,
    )
    await crawler.crawl()
    assert len(snapshots) == 3
    assert snapshots[-1] == {"fetched": 3, "cached": 0, "failed": 0, "pending": 0}


@pytest.mark.asyncio
async def test_crawler_callback_may_return_single_request_or_item(run_test_site, tmp_path):
    base_url = await run_test_site()
    pipeline = ItemPipeline(output_dir=tmp_path)
    crawler = Crawler(start_urls=[f"{base_url}/page1"], pipeline=pipeline, cache_type="none")

    def parse_page2(response: Response):
        return {"title": response.xpath("//h1/text()")[0]}

    @crawler.on_response
    def parse(response: Response):
        return Request(response.urljoin("/page2"), callback=parse_page2)

    await crawler.crawl()
    lines = (tmp_path / "items.jsonl").read_text().splitlines()
    assert lines == ['{"title": "Page 2"}']


@pytest.mark.asyncio
async def test_crawler_from_spider_uses_spider_settings(run_test_site, tmp_path):
    base_url = await run_test_site()
    seen = []

    class MySpider(Spider):
        start_urls = [f"{base_url}/page1"]
        allowed_uris = ["nothing.invalid"]
        max_depth = 7

        async def parse(self, response: Response):
            seen.append(response.url)
            yield {"url": response.url, "h1": response.xpath("//h1/text()")[0]}

    spider = MySpider()
    crawler = Crawler.from_spider(spider, pipeline=ItemPipeline(tmp_path), cache_type="none")
    assert crawler.start_urls == spider.start_urls
    assert crawler.allowed_uris == ["nothing.invalid"]
    assert crawler.max_depth == 7
    await crawler.crawl()

    assert seen == [f"{base_url}/page1"]
    rows = [json.loads(line) for line in (tmp_path / "items.jsonl").read_text().splitlines()]
    assert rows == [{"url": f"{base_url}/page1", "h1": "Page 1"}]


@pytest.mark.asyncio
async def test_crawler_does_not_refetch_resources_captured_with_the_page(tmp_path):
    """A browser backend captures sub-resources while rendering; the extractor must not enqueue them again."""
    from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse

    fetched = []

    class FakeBrowser:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetch(self, url, **kwargs):
            fetched.append(url)
            html = b'<html><body><img src="/logo.png"><img src="/lazy.png" loading="lazy"></body></html>'
            transactions = []
            for res_url, body, content_type in [
                (url, html, b"text/html"),
                (url + "logo.png", b"PNG", b"image/png"),
                (url + "lazy.png", b"PNG2", b"image/png"),
            ]:
                transactions.append(
                    NetworkTransaction(
                        request=RawRequest(url=res_url),
                        response=RawResponse(
                            status_code=200, reason_phrase="OK", http_version="HTTP/1.1",
                            raw_headers=[(b"Content-Type", content_type)], raw_body=body,
                        ),
                    )
                )
            # the rendered DOM differs from the wire HTML: JS added a link and an image
            rendered = (
                '<html><body><img src="/logo.png"><img src="/lazy.png" loading="lazy">'
                '<img src="/added-by-js.png"><a href="/next">next</a></body></html>'
            )
            page = Page(url=url, status_code=200, content=rendered, raw_body=html, transactions=transactions, main_transaction=transactions[0])
            return CrawlResult(transactions=transactions, page=page)

    crawler = Crawler(
        start_urls=["http://site.test/"], max_depth=1, allowed_uris=["site.test"], fetcher=FakeBrowser(),
        warc_filename=tmp_path / "o.warc", cache_type="none",
    )
    await crawler.crawl()

    # links come from the rendered DOM (the browser's last transaction is an asset, not the document),
    # resources the browser already loaded are not fetched again, new ones are
    assert fetched[0] == "http://site.test/"
    assert set(fetched) == {"http://site.test/", "http://site.test/added-by-js.png", "http://site.test/next"}
    with open(tmp_path / "o.warc", "rb") as fobj:
        uris = [r.rec_headers.get_header("WARC-Target-URI") for r in ArchiveIterator(fobj) if r.rec_type == "response"]
    assert uris[:3] == ["http://site.test/", "http://site.test/logo.png", "http://site.test/lazy.png"]
    # the extractor never fetched logo.png/lazy.png on their own: every record of them came with a page
    assert uris.count("http://site.test/logo.png") == 1
    assert sum(1 for uri in uris if uri.endswith("logo.png")) == len(fetched)
