#!/usr/bin/env python3
"""One browser, many pages at once: each URL gets its own tab and they load concurrently.

    python examples/browser_multi_tab.py https://example.com https://example.org

Prints one JSON line per URL (JSONL), in the order given. Lightpanda has a single tab, so there the URLs are visited
one after the other in the same page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a checkout without installing crau

from crau.browser import Browser, BrowserError  # noqa: E402


async def visit(tab, url: str) -> dict:
    try:
        response = await tab.goto(url, wait_idle=True)
        links = await tab.evaluate("document.querySelectorAll('a[href]').length")
        return {"url": url, "status": response.status if response else None, "title": await tab.title(), "links": links}
    except BrowserError as exc:
        return {"url": url, "error": str(exc)}


async def run(urls: list[str], browser_name: str) -> list[dict]:
    async with Browser(browser_name, timeout=30) as browser:
        if browser_name == "lightpanda":
            tab = await browser.new_tab()
            return [await visit(tab, url) for url in urls]
        tabs = [await browser.new_tab() for _ in urls]
        return list(await asyncio.gather(*(visit(tab, url) for tab, url in zip(tabs, urls, strict=True))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="+", help="URLs to open")
    parser.add_argument("-b", "--browser", metavar="name", default="chromium", help="chromium, firefox or lightpanda")
    args = parser.parse_args()
    try:
        rows = asyncio.run(run(args.url, args.browser))
    except KeyboardInterrupt:
        sys.exit(130)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
