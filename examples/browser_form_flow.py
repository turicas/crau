#!/usr/bin/env python3
"""Multi-step navigation with the library: map a page, fill a form, submit, wait for the result, keep evidence.

    python examples/browser_form_flow.py http://127.0.0.1:8765 out/ --browser firefox

Written against tests/browser/testsite.py (`python tests/browser/testsite.py 8765`); adapt targets and waits to the site you drive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a checkout without installing crau

from crau.browser import Browser, WaitTimeout  # noqa: E402


async def run(base_url: str, output: Path, browser_name: str) -> dict:
    async with Browser(browser_name, timeout=20) as browser:
        tab = await browser.new_tab(f"{base_url}/form", wait_selector="form")

        # 1. look before acting: the outline lists fields and buttons with selectors that fill() and click() accept
        outline = await tab.outline()
        fields = [item for item in outline["items"] if item["role"] in ("textbox", "combobox", "checkbox")]

        # 2. act like a user; targets can be CSS selectors, text=... or label=...
        await tab.fill("label=User name", "alice")
        await tab.fill("label=Password", "s3cret")
        await tab.fill("label=Colour", "Green")
        await tab.fill("label=Remember me", True)
        await tab.click("text=Sign in")

        # 3. wait for the outcome instead of sleeping; a timeout is an answer too
        try:
            await tab.wait(js="document.title === 'signed-in'", timeout=10)
        except WaitTimeout:
            await tab.screenshot(output / "failure.png", full_page=True)
            raise

        # 4. read structured data straight from the page and keep evidence
        received = json.loads(await tab.evaluate("document.querySelector('#result').textContent"))
        await tab.screenshot(output / "signed-in.png", selector="#result")
        return {"fields": [item["name"] for item in fields], "received": received, "title": await tab.title()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", help="Base URL of the test site, without a trailing slash")
    parser.add_argument("output", type=Path, help="Directory for the screenshots")
    parser.add_argument("-b", "--browser", metavar="name", default="chromium", help="chromium, firefox or lightpanda")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.base_url.rstrip("/"), args.output, args.browser))
    except KeyboardInterrupt:
        sys.exit(130)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
