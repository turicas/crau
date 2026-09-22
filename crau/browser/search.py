"""Search the web through a real browser and print the organic results (DuckDuckGo or Bing).

Results are read from the rendered page through `crau.browser`, so there is no API key and no HTML parsing here;
redirect links are decoded to the real target URL.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

ENGINES = {
    "duckduckgo": {
        "url": "https://html.duckduckgo.com/html/?q={query}",
        "ready": ".result, .no-results",
        "extract": """() => [...document.querySelectorAll(".result")]
            .filter((item) => !item.className.includes("result--ad"))
            .map((item) => {
              const link = item.querySelector("a.result__a");
              const snippet = item.querySelector(".result__snippet");
              if (!link) return null;
              return {title: link.textContent, url: link.href, snippet: snippet ? snippet.textContent : ""};
            }).filter(Boolean)""",
    },
    "bing": {
        "url": "https://www.bing.com/search?q={query}",
        "ready": "li.b_algo, .b_no",
        "extract": """() => [...document.querySelectorAll("li.b_algo")].map((item) => {
              const link = item.querySelector("h2 a");
              const snippet = item.querySelector(".b_caption p, p");
              if (!link) return null;
              return {title: link.textContent, url: link.href, snippet: snippet ? snippet.textContent : ""};
            }).filter(Boolean)""",
    },
}
BROWSER_CHOICES = ("chromium", "firefox", "lightpanda")


def decode_result_url(url: str) -> str | None:
    """Real target of a result link; None for ads. DuckDuckGo wraps targets in `uddg=`, Bing in `u=a1<base64url>`."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    host = parts.netloc.lower()
    if host.endswith("duckduckgo.com"):
        if parts.path.startswith("/y.js") or "ad_provider" in query:
            return None
        if parts.path.startswith("/l/") and query.get("uddg"):
            return query["uddg"][0]
    if host.endswith("bing.com") and parts.path.startswith("/ck/") and query.get("u"):
        encoded = query["u"][0]
        if encoded.startswith("a1"):
            padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            try:
                decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return url
            return decoded if decoded.startswith(("http://", "https://")) else url
    return url


def clean_results(raw: list[dict], limit: int) -> list[dict]:
    """Decode links, collapse whitespace, drop ads and duplicates, number what is left."""
    results, seen = [], set()
    for item in raw:
        url = decode_result_url(item.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "position": len(results) + 1,
                "title": " ".join(item.get("title", "").split()),
                "url": url,
                "snippet": " ".join(item.get("snippet", "").split()),
            }
        )
        if len(results) == limit:
            break
    return results


async def search(
    query: str, engine: str = "duckduckgo", limit: int = 10, serp_url: str | None = None, **browser_options
) -> list[dict]:
    """Run the search and return `[{position, title, url, snippet}]`. `browser_options` go to `browser.Browser`."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from crau.browser import Browser

    settings = ENGINES[engine]
    url = (serp_url or settings["url"]).format(query=quote_plus(query))
    async with Browser(**browser_options) as browser:
        tab = await browser.new_tab(url, wait_selector=settings["ready"])
        return clean_results(await tab.call_function(settings["extract"]), limit)


def positive_int(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got {value!r}")
    return int(value)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="What to search for")
    parser.add_argument(
        "-e", "--engine", metavar="name", choices=sorted(ENGINES), default="duckduckgo",
        help=f"Search engine. Options: {', '.join(sorted(ENGINES))} (default: duckduckgo)",
    )  # fmt: skip
    parser.add_argument("-n", "--limit", metavar="n", type=positive_int, default=10, help="Maximum results (default: 10)")
    parser.add_argument("-J", "--json", action="store_true", help="Print JSON instead of text")
    parser.add_argument(
        "-b", "--browser", metavar="name", choices=BROWSER_CHOICES, default="chromium",
        help=f"Browser to use. Options: {', '.join(BROWSER_CHOICES)} (default: chromium)",
    )  # fmt: skip
    parser.add_argument("-p", "--profile", metavar="dir", type=Path, help="Persistent profile directory")
    parser.add_argument("-k", "--insecure", action="store_true", help="Accept invalid TLS certificates")
    parser.add_argument("-t", "--timeout", metavar="seconds", type=float, default=30.0, help="Default: 30")
    parser.add_argument(
        "-u", "--serp-url", metavar="url", help="Results page URL with {query} in it (self-hosted front ends, tests)"
    )


def run(args: argparse.Namespace) -> int:
    """Exit codes: 0 results printed, 1 no results (blocked, empty or the layout changed) or another error,
    3 browser missing or failed to start, 4 page could not be loaded, 130 interrupted."""
    import asyncio
    import json

    from .errors import BrowserError, LaunchError, NavigationError

    try:
        results = asyncio.run(
            search(
                args.query, args.engine, args.limit, args.serp_url,
                browser=args.browser, profile=args.profile, insecure=args.insecure, timeout=args.timeout,
            )
        )  # fmt: skip
    except KeyboardInterrupt:
        print("\ncrau: interrupted", file=sys.stderr)
        return 130
    except BrowserError as exc:
        print(f"crau: error: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, LaunchError) else 4 if isinstance(exc, NavigationError) else 1
    if not results:
        print("crau: error: no results (blocked, empty or the page layout changed)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for item in results:
            print(f"{item['position']}. {item['title']}\n   {item['url']}\n   {item['snippet']}")
    return 0
