#!/usr/bin/env python3
"""Keep one browser alive across separate script runs: log in once, come back later to the same tabs and cookies.

crau browser start work
python examples/browser_reuse_instance.py work http://127.0.0.1:8765/form     # run it twice: the second run finds the tab
crau browser stop work
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a checkout without installing crau

from crau.browser import Browser  # noqa: E402


async def run(instance: str, url: str) -> dict:
    async with Browser.attach(instance) as browser:  # leaving the block detaches; the browser keeps running
        for tab in await browser.tabs():
            if (await tab.info())["url"] == url:
                visits = await tab.evaluate("window.visits = (window.visits || 1) + 1")
                return {"reused": True, "visits": visits, "tabs": len(await browser.tabs())}
        tab = await browser.new_tab(url)
        await tab.evaluate("window.visits = 1")
        return {"reused": False, "visits": 1, "tabs": len(await browser.tabs())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", help="Name given to 'crau browser start'")
    parser.add_argument("url", help="Page to open, or to find among the open tabs")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args.instance, args.url))))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
