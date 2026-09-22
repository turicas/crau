---
name: crau
description: Drive a real browser (Chromium, Firefox or Lightpanda) and archive the web with the `crau` command and Python library - open pages and get the rendered HTML or text, run JavaScript, take screenshots or PDFs, fill forms and click through multi-step flows, keep a logged-in browser alive across commands, reuse cookies, profiles and custom headers, capture all network traffic to a HAR file to discover the API behind a page, search the web, and crawl or archive sites to WARC/HAR. Use this skill whenever a task needs what a plain HTTP request cannot give - JavaScript-rendered content, interaction, visual evidence, login state, anti-bot friction - or whenever the user mentions browsing, scraping, screenshots, headless browsers, CDP, WebDriver BiDi, Playwright/Puppeteer/Selenium-style automation, HAR or WARC files or web archiving, even if they do not name a tool.
---

# crau: browser control and web archiving

`crau` drives real browsers over their native protocols (CDP for Chromium and Lightpanda, WebDriver BiDi for Firefox)
using only the Python standard library, and archives sites to WARC or HAR. This text ships inside the package:
`crau skill` always prints the version that matches the installed commands.

Setup: `pipx install crau` (or `pip install crau`; Python >= 3.10, no dependencies), then
`crau browser install chromium` if no browser is installed, and `crau browser doctor` to see what was found.
Archiving and crawling need the extras: `pip install 'crau[archive]'`.

| Need | Use |
|---|---|
| One thing from one page (HTML, text, a value, a screenshot, a PDF, a HAR) | the CLI: `crau page ...`, `crau har ...` |
| Several steps on a site (log in, fill, click, paginate, extract) | the library: `from crau.browser import Browser` |
| Several commands against the same logged-in browser | `crau browser start NAME`, then `-i NAME` |
| Web search results | `crau search "query"` |
| A copy of a site or of a list of URLs (WARC/HAR) | `crau archive ...` - see `crau skill -r archiving` |

## Choosing a browser (`-b`, default chromium)

- **chromium**: the default. Everything works, including PDF header/footer templates.
- **firefox**: everything works on current releases. ESR 140 has no response bodies in HAR and no extra headers.
- **lightpanda**: starts in ~50 ms and uses little memory, but has no rendering: no real screenshots, no PDF, a single
  tab, no named instances. Use it for fast HTML/text/JS extraction, and fall back to chromium when a page misbehaves.

Stealth is on by default (automation fingerprints hidden, regular User-Agent); `-S` turns it off.

## CLI

```bash
crau page html URL                      # rendered HTML to stdout (-o FILE to save)
crau page text URL                      # visible text
crau page js URL "document.title"       # any expression or promise -> JSON on stdout
crau page outline URL                   # headings, links, buttons, fields: role, name, CSS selector
crau page screenshot URL shot.png -f    # -f full page, -l SELECTOR one element, .jpg + -Q 80 for JPEG
crau page pdf URL page.pdf
crau page cookies URL -o cookies.txt    # the browser's cookies after loading the page (Netscape format, -J for JSON)
crau har capture URL [URL...] out.har   # every request and response, nothing removed
crau har summary out.har -M json        # endpoints + shape of bodies, no values (cheap to read)
```

Waiting happens in this order after navigation, on every page command: `-s TARGET` (element exists), `-j EXPR`
(expression truthy), `-I` (network idle), `-r` (scroll to the bottom for lazy content), `-w MS` (fixed delay). Then
each `-e JS` runs, then the command captures. Prefer `-s`/`-j` over `-w`.

State and identity: `-p DIR` persistent profile (keeps logins), `-c FILE` cookies (Netscape `cookies.txt` or JSON),
`-H "Name: value"` or `-F FILE` extra headers (use the file for secrets: arguments show up in `ps`), `-k` accept
invalid TLS certificates (intercepting proxies), `-V 1280x800` viewport, `-t SECONDS` timeout.

Data goes to stdout, diagnostics to stderr (`-v` for timings, `-C` for the page console), success is silent.
Exit codes: 0 ok, 1 other error, 2 usage, 3 browser missing/failed to start, 4 page could not be loaded, 5 wait
timed out, 6 JavaScript raised, 7 unsupported by this browser, 8 HTTP status >= 400 (only with `-x`), 130 interrupted.

### Same browser across commands

```bash
crau browser start work -b firefox -p ~/profiles/work
crau tab open work https://example.com/login        # prints the tab index
crau page js tab:1 "document.title" -i work         # tab:N = act on that tab, do not navigate
crau page screenshot tab:1 state.png -i work
crau tab list work
crau browser status
crau browser stop work
```

## Library (multi-step navigation)

Write a short script instead of chaining CLI calls. The library handles launching, a free port, profile, cleanup on
errors and signals, waiting, and gives the same API on the three browsers.

```python
import asyncio
from crau.browser import Browser, WaitTimeout

async def main():
    async with Browser("chromium", profile="~/profiles/shop", timeout=30) as browser:
        tab = await browser.new_tab("https://shop.example/login", wait_selector="form")
        print(await tab.outline())                  # look first: fields and buttons with usable selectors
        await tab.fill("label=E-mail", "me@example.com")     # targets: CSS, text=..., label=...
        await tab.fill("label=Password", "secret")
        await tab.click("text=Sign in")             # real mouse events, scrolls into view
        await tab.wait(js="location.pathname === '/account'", timeout=15)
        orders = await browser.new_tab("https://shop.example/orders", wait_idle=True)
        rows = await orders.evaluate("[...document.querySelectorAll('tr.order')].map(r => r.innerText)")
        await orders.screenshot("orders.png", full_page=True)
        print(rows)

asyncio.run(main())
```

The repository's `examples/` directory has complete, tested scripts: `browser_form_flow.py`, `browser_multi_tab.py`
(concurrent tabs), `browser_capture_har.py` (find the API behind a page), `browser_reuse_instance.py` (attach to a
running browser). Copy the closest one.

Rules that avoid the usual failures:
- Wait for a condition (`wait_selector`, `tab.wait(js=...)`), not for time. A `WaitTimeout` tells you the page is not
  in the state you assumed: take a screenshot or read `tab.text()` before retrying.
- `goto()` returns the main document's `Response` (`status`, `headers`, `ok`); HTTP errors do not raise.
- `evaluate()` returns plain Python data and raises `JsError` with the page's message. Return data, not DOM nodes.
- For anything not wrapped, send raw protocol commands: `await tab.cdp("Domain.method", {...})` or `tab.bidi(...)`.

## References (read only when needed): `crau skill -r NAME`, `crau skill -l` lists them

- `library` - full Python API, every option and error.
- `network-capture` - HAR capture, what each browser provides, from HAR to a direct API client.
- `archiving` - `crau archive`, backends and formats, reading WARC files, crawling from Python.
- `cdp` - tested raw CDP recipes: accessibility tree, iframes, blocking URLs, downloads, pipe transport.
- `bidi` - tested raw BiDi recipes: accessibility locators, iframes, intercepts, preload scripts.
- `lightpanda` - what Lightpanda can and cannot do, and its quirks.
- `pitfalls` - containers (HOME, core dumps, zombies, TLS proxies), install notes, troubleshooting.
