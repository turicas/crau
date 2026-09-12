from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from warcio.archiveiterator import ArchiveIterator

from crau.cli import main
from crau.crawler import Crawler
from crau.utils import WarcReader


async def _run_smoke_warc(url: str, output_path: Path) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        warc_filename=output_path,
        cache_type="none",
        timeout=10.0,
    )
    await crawler.crawl()

    assert output_path.exists(), f"WARC file {output_path} was not created"
    reader = WarcReader(str(output_path))
    records = list(reader)
    rec_types = [r.rec_type for r in records]
    assert "request" in rec_types, "WARC missing request record"
    assert "response" in rec_types, "WARC missing response record"

    resp = reader.get_response(url)
    assert resp is not None, f"Could not find response for {url}"
    body = resp.content_stream().read()
    assert len(body) > 0, "Response body is empty"


async def _run_smoke_har_raw(url: str, output_path: Path) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        har_filename=output_path,
        format="har",
        cache_type="none",
        timeout=10.0,
    )
    await crawler.crawl()

    assert output_path.exists(), f"HAR file {output_path} was not created"
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert "log" in data
    assert data["log"]["version"] == "1.2"
    entries = data["log"]["entries"]
    assert len(entries) >= 1
    assert entries[0]["response"]["status"] == 200
    assert "text" in entries[0]["response"]["content"]


async def _run_smoke_rendered_har(url: str, output_path: Path) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        har_filename=output_path,
        format="rendered-har",
        cache_type="none",
        timeout=10.0,
    )
    await crawler.crawl()

    assert output_path.exists(), f"Rendered HAR file {output_path} was not created"
    data = json.loads(output_path.read_text(encoding="utf-8"))
    entries = data["log"]["entries"]
    assert len(entries) >= 1
    page_entry = [e for e in entries if e["request"]["url"].rstrip("/") == url.rstrip("/")][0]
    content_text = page_entry["response"]["content"]["text"]
    assert len(content_text) > 0
    assert "<html" in content_text.lower()


async def _run_smoke_rendered_only_har(url: str, output_path: Path) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        har_filename=output_path,
        format="rendered-only-har",
        cache_type="none",
        timeout=10.0,
    )
    await crawler.crawl()

    assert output_path.exists(), f"Rendered-only HAR file {output_path} was not created"
    data = json.loads(output_path.read_text(encoding="utf-8"))
    entries = data["log"]["entries"]
    assert len(entries) == 1, f"Expected exactly 1 target page entry, got {len(entries)}"
    assert "<html" in entries[0]["response"]["content"]["text"].lower()


def run_smoke_tests(target_url: str = "https://example.com") -> bool:
    """Run end-to-end smoke tests against a known URL or local mock."""
    start_total = time.perf_counter()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        warc_file = tmp_path / "smoke.warc.gz"
        har_raw_file = tmp_path / "smoke_raw.har"
        har_rendered_file = tmp_path / "smoke_rendered.har"
        har_rendered_only_file = tmp_path / "smoke_rendered_only.har"

        print(f"1. Testing WARC archive against {target_url}...")
        t0 = time.perf_counter()
        asyncio.run(_run_smoke_warc(target_url, warc_file))
        print(f"   [OK] WARC created ({time.perf_counter() - t0:.2f}s)")

        print("2. Testing CLI 'list' and 'extract' commands...")
        t0 = time.perf_counter()
        exit_code = main(["list", str(warc_file)])
        assert exit_code == 0, f"CLI list failed with code {exit_code}"

        extract_file = tmp_path / "extracted.html"
        exit_code = main(["extract", str(warc_file), target_url, str(extract_file)])
        assert exit_code == 0, f"CLI extract failed with code {exit_code}"
        assert extract_file.exists() and extract_file.stat().st_size > 0
        print(f"   [OK] CLI list & extract ({time.perf_counter() - t0:.2f}s)")

        print(f"3. Testing raw HAR format (--format har)...")
        t0 = time.perf_counter()
        asyncio.run(_run_smoke_har_raw(target_url, har_raw_file))
        print(f"   [OK] Raw HAR created ({time.perf_counter() - t0:.2f}s)")

        print(f"4. Testing rendered HAR format (--format rendered-har)...")
        t0 = time.perf_counter()
        asyncio.run(_run_smoke_rendered_har(target_url, har_rendered_file))
        print(f"   [OK] Rendered HAR created ({time.perf_counter() - t0:.2f}s)")

        print(f"5. Testing rendered-only HAR format (--format rendered-only-har)...")
        t0 = time.perf_counter()
        asyncio.run(_run_smoke_rendered_only_har(target_url, har_rendered_only_file))
        print(f"   [OK] Rendered-only HAR created ({time.perf_counter() - t0:.2f}s)")

    duration = time.perf_counter() - start_total
    print(f"\nAll smoke tests passed successfully in {duration:.2f}s!")
    return True
