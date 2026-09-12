from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from crau.cli import main
from crau.crawler import Crawler
from crau.utils import WarcReader


def is_browser_available(backend: str) -> bool:
    """Check if the binary required by the browser backend is installed in PATH."""
    if backend == "http":
        return True
    elif backend == "chromium":
        return any(
            shutil.which(cmd)
            for cmd in ["chromium", "google-chrome", "chromium-browser", "chrome"]
        )
    elif backend == "firefox":
        return any(shutil.which(cmd) for cmd in ["firefox-esr", "firefox"])
    elif backend == "lightpanda":
        return shutil.which("lightpanda") is not None
    return False


async def _run_smoke_warc(
    url: str,
    output_path: Path,
    backend: str = "http",
    user_data_dir: Path | None = None,
    binary_path: Path | None = None,
) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        backend=backend,
        warc_filename=output_path,
        cache_type="none",
        timeout=20.0,
        user_data_dir=user_data_dir,
        binary_path=binary_path,
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
    # Content check for known sites like Wikipedia
    if "wikipedia.org" in url:
        assert b"Wikipedia" in body, "Expected 'Wikipedia' in WARC response payload"


async def _run_smoke_har_raw(
    url: str,
    output_path: Path,
    backend: str = "http",
    user_data_dir: Path | None = None,
    binary_path: Path | None = None,
) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        backend=backend,
        har_filename=output_path,
        format="har",
        cache_type="none",
        timeout=20.0,
        user_data_dir=user_data_dir,
        binary_path=binary_path,
    )
    await crawler.crawl()

    assert output_path.exists(), f"HAR file {output_path} was not created"
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert "log" in data
    assert data["log"]["version"] == "1.2"
    entries = data["log"]["entries"]
    assert len(entries) >= 1
    main_entry = entries[0]
    assert main_entry["response"]["status"] == 200
    content_text = main_entry["response"]["content"]["text"]
    assert len(content_text) > 0
    if "wikipedia.org" in url:
        assert "Wikipedia" in content_text, "Expected 'Wikipedia' in raw HAR content"


async def _run_smoke_rendered_har(
    url: str,
    output_path: Path,
    backend: str = "http",
    user_data_dir: Path | None = None,
    binary_path: Path | None = None,
) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        backend=backend,
        har_filename=output_path,
        format="rendered-har",
        cache_type="none",
        timeout=20.0,
        user_data_dir=user_data_dir,
        binary_path=binary_path,
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
    if "wikipedia.org" in url:
        assert "Wikipedia" in content_text, "Expected 'Wikipedia' in rendered HAR content"


async def _run_smoke_rendered_only_har(
    url: str,
    output_path: Path,
    backend: str = "http",
    user_data_dir: Path | None = None,
    binary_path: Path | None = None,
) -> None:
    crawler = Crawler(
        start_urls=[url],
        max_depth=0,
        backend=backend,
        har_filename=output_path,
        format="rendered-only-har",
        cache_type="none",
        timeout=20.0,
        user_data_dir=user_data_dir,
        binary_path=binary_path,
    )
    await crawler.crawl()

    assert output_path.exists(), f"Rendered-only HAR file {output_path} was not created"
    data = json.loads(output_path.read_text(encoding="utf-8"))
    entries = data["log"]["entries"]
    # Exactly 1 target page entry without secondary assets
    assert len(entries) == 1, f"Expected exactly 1 target page entry, got {len(entries)}"
    page_entry = entries[0]
    content_text = page_entry["response"]["content"]["text"]
    assert "<html" in content_text.lower()
    if "wikipedia.org" in url:
        assert "Wikipedia" in content_text, "Expected 'Wikipedia' in rendered-only HAR"


def run_smoke_tests(
    target_url: str = "https://en.wikipedia.org/wiki/Main_Page",
    backend: str = "http",
    user_data_dir: Path | None = None,
    binary_path: Path | None = None,
) -> bool:
    """Run end-to-end smoke tests verifying WARC, list, extract and all HAR formats."""
    if not is_browser_available(backend):
        print(f"Skipping {backend}: browser executable is not installed on this system.")
        return False

    start_total = time.perf_counter()
    print(f"=== Running crau smoke tests ({backend}) against: {target_url} ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        warc_file = tmp_path / "smoke.warc.gz"
        har_raw_file = tmp_path / "smoke_raw.har"
        har_rendered_file = tmp_path / "smoke_rendered.har"
        har_rendered_only_file = tmp_path / "smoke_rendered_only.har"

        print(f"1. Testing WARC archive...")
        t0 = time.perf_counter()
        asyncio.run(
            _run_smoke_warc(
                target_url,
                warc_file,
                backend=backend,
                user_data_dir=user_data_dir,
                binary_path=binary_path,
            )
        )
        print(f"   [OK] WARC created ({time.perf_counter() - t0:.2f}s)")

        print("2. Testing CLI 'list' and 'extract' commands...")
        t0 = time.perf_counter()
        exit_code = main(["list", str(warc_file)])
        assert exit_code == 0, f"CLI list failed with code {exit_code}"

        extract_file = tmp_path / "extracted.html"
        exit_code = main(["extract", str(warc_file), target_url, str(extract_file)])
        assert exit_code == 0, f"CLI extract failed with code {exit_code}"
        assert extract_file.exists() and extract_file.stat().st_size > 0
        if "wikipedia.org" in target_url:
            assert "Wikipedia" in extract_file.read_text(encoding="utf-8", errors="replace")
        print(f"   [OK] CLI list & extract ({time.perf_counter() - t0:.2f}s)")

        print("3. Testing raw HAR format (--format har)...")
        t0 = time.perf_counter()
        asyncio.run(
            _run_smoke_har_raw(
                target_url,
                har_raw_file,
                backend=backend,
                user_data_dir=user_data_dir,
                binary_path=binary_path,
            )
        )
        print(f"   [OK] Raw HAR created ({time.perf_counter() - t0:.2f}s)")

        print("4. Testing rendered HAR format (--format rendered-har)...")
        t0 = time.perf_counter()
        asyncio.run(
            _run_smoke_rendered_har(
                target_url,
                har_rendered_file,
                backend=backend,
                user_data_dir=user_data_dir,
                binary_path=binary_path,
            )
        )
        print(f"   [OK] Rendered HAR created ({time.perf_counter() - t0:.2f}s)")

        print("5. Testing rendered-only HAR format (--format rendered-only-har)...")
        t0 = time.perf_counter()
        asyncio.run(
            _run_smoke_rendered_only_har(
                target_url,
                har_rendered_only_file,
                backend=backend,
                user_data_dir=user_data_dir,
                binary_path=binary_path,
            )
        )
        print(f"   [OK] Rendered-only HAR created ({time.perf_counter() - t0:.2f}s)")

    duration = time.perf_counter() - start_total
    print(f"All smoke tests for '{backend}' passed successfully in {duration:.2f}s!\n")
    return True
