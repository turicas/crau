import asyncio
from dataclasses import dataclass
from pathlib import Path
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
        for r in ArchiveIterator(fobj):
            if r.rec_type == "response":
                records.append(r.rec_headers.get_header("WARC-Target-URI"))

    assert f"{base_url}/page1" in records
    assert f"{base_url}/img/pic.png" in records
    assert f"{base_url}/page2" in records


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
    pipeline = ItemPipeline(output_dir=tmp_path)

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
