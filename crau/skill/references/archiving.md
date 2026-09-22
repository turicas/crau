# Archiving and crawling

Needs the archiving extras: `pip install 'crau[archive]'` (h11, lxml, tqdm, warcio). The browser commands
described in `SKILL.md` do not.

## Archive pages to WARC or HAR

```bash
crau archive site.warc.gz https://example.com/ https://example.org/page      # URLs on the command line
crau archive site.warc.gz -i urls.txt                                         # or one URL per line in a file
```

Options worth knowing (`crau archive --help` has all of them):

- `--max-depth N` (default 1): link hops to follow from the start URLs. Page dependencies (images, CSS, JS, fonts
  referenced by stylesheets, icons) are always fetched regardless of depth.
- `--allowed-uris DOMAIN-OR-PREFIX` (repeatable): only follow links under these.
- `--concurrency` / `--concurrency-per-domain` (default 16 / 4), `--autothrottle` (default on) and `--timeout` control
  how hard servers are hit; `--max-retries N` (default 3) retries with exponential backoff.
- `--cache sqlite --cache-path FILE`: reuse responses from a previous run (off by default: archiving means capturing
  the site now).
- `-q` hides the progress bar; `--verbose` logs retries and cache hits. Both go before the command:
  `crau -q archive ...`.

## Backends and formats

```bash
crau archive out.warc.gz https://example.com                                         # wire-level HTTP/1.1 (default)
crau archive out.har https://example.com --backend chromium --format har            # headless Chromium (CDP)
crau archive out.har https://example.com --backend firefox --format rendered-har    # headless Firefox (BiDi)
crau archive out.har https://example.com --backend lightpanda --format rendered-only-har
```

- `warc`: ISO WARC. With the `http` backend records hold the bytes exactly as received (chunked framing included), plus
  `warcinfo`, `WARC-IP-Address` and `WARC-Concurrent-To`.
- `har`: every network transaction.
- `rendered-har`: every transaction, with the target pages replaced by the JavaScript-rendered DOM.
- `rendered-only-har`: only the target pages, rendered.

With a browser backend everything the page requests is archived: after the load event crau keeps capturing for up to
`--browser-settle` seconds (default 10), scrolling one viewport at a time to trigger lazy content, and stops once the
network has been idle for half a second (`--no-auto-scroll` only waits, `--browser-settle 0` collects right after
load). `--user-data-dir DIR` persists cookies and sessions. WebSocket and Server-Sent Events traffic is not captured,
nor anything that needs a click: for that, drive the page with `crau.browser` and record with `tab.start_recording()`
(see `network-capture.md`).

## Read an archive

```bash
crau list site.warc.gz                                                   # URLs stored in the WARC
crau extract site.warc.gz https://example.com/page.html page.html         # one response body to a file
crau play site.warc.gz                                                   # local replay server; needs crau[play]
crau pack https://example.com/ mirror-directory-or-archive site.warc.gz   # files from wget -r / httrack into a WARC
```

## Crawl with Python

```python
from crau import Crawler, Spider          # see the project README for a complete spider
```

`crau archive` and `crau har capture` answer different needs: `archive` crawls many URLs into WARC or HAR with
retries, throttling and link following; `har capture` records exactly what one browser tab did, interaction included.
