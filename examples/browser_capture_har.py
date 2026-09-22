#!/usr/bin/env python3
"""Find the API behind a page: record everything while interacting, save the HAR, print the JSON endpoints' shapes.

python examples/browser_capture_har.py http://127.0.0.1:8765 site.har
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a checkout without installing crau

from crau.browser import Browser  # noqa: E402
from crau.browser.har import summarize_har  # noqa: E402


async def run(base_url: str, output: Path, browser_name: str) -> list[dict]:
    async with Browser(browser_name, timeout=20) as browser:
        tab = await browser.new_tab()
        if not await tab.start_recording():
            print("warning: this browser cannot provide response bodies", file=sys.stderr)
        await tab.goto(f"{base_url}/form")
        await tab.fill("label=User name", "alice")
        await tab.click("text=Sign in")  # the interesting request only happens after the click
        await tab.wait(js="document.title === 'signed-in'", idle=True)
        har = await tab.har()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(har, indent=2, ensure_ascii=False), encoding="utf-8")
    return summarize_har(har, mime_filter="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", help="Base URL of the test site, without a trailing slash")
    parser.add_argument("output", type=Path, help="HAR file to write")
    parser.add_argument("-b", "--browser", metavar="name", default="chromium", help="chromium, firefox or lightpanda")
    args = parser.parse_args()
    try:
        rows = asyncio.run(run(args.base_url.rstrip("/"), args.output, args.browser))
    except KeyboardInterrupt:
        sys.exit(130)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
