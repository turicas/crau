# crau: Easy-to-use Web Archiver

*crau* is the way (most) Brazilians pronounce *crawl*, it's the easiest
command-line tool for archiving the Web and playing archives: you just need a
list of URLs.

## Installation

`pip install 'crau[archive]'` (Python 3.10+) for archiving and crawling. A bare
`pip install crau` installs no dependency at all: it is enough for everything that runs on
the standard library, and the archiving commands tell you which extra is missing.

- `crau[archive]`: archiving and crawling, with any backend (h11, lxml, tqdm, warcio); the
  browser backends themselves run on the standard library
- `crau[play]`: the above plus [pywb](https://github.com/webrecorder/pywb) for `crau play`
  (pywb itself only supports Python 3.7-3.12)
- `crau[lightpanda]`: the above plus the Lightpanda binary package
- `crau[browser]`: kept as an alias of `crau[archive]`
- `crau[all]`: everything above


## Running

### Archiving

Archive a list of URLs by passing them via command-line:

```bash
crau archive myarchive.warc.gz http://example.com/page-1 http://example.org/page-2 ... http://example.net/page-N
```

or passing a text file (one URL per line):

```bash
echo "http://example.com/page-1" > urls.txt
echo "http://example.org/page-2" >> urls.txt
echo "http://example.net/page-N" >> urls.txt

crau archive myarchive.warc.gz -i urls.txt
```

Run `crau archive --help` for more options. Useful ones:

- `--max-depth N` (default 1): how many link hops to follow from the start URLs.
  Page dependencies (images, CSS, JS, fonts referenced by stylesheets, icons) are
  always fetched regardless of depth.
- `--allowed-uris DOMAIN-OR-PREFIX` (repeatable): only follow links under these.
- `--concurrency` / `--concurrency-per-domain` (default 16 / 4), `--autothrottle`
  (default on) and `--timeout` control how hard the crawler hits servers.
- `--max-retries N` (default 3): retries with exponential backoff on network errors.
- `--cache sqlite --cache-path FILE`: reuse responses from a previous run instead of
  fetching them again (off by default: archiving means capturing the site now).
- `-q`/`--quiet` hides the progress bar; `--verbose` logs retries and cache hits.

### Choosing Backend and Archive Format

crau supports pluggable backends and archival formats:

```bash
# 1. Wire-level HTTP/1.1 (default, fast, exact bytes preservation)
crau archive archive.warc.gz https://example.com

# 2. Headless Chromium via CDP
crau archive archive.har https://example.com --backend chromium --format har

# 3. Headless Firefox via WebDriver BiDi
crau archive archive.har https://example.com --backend firefox --format rendered-har

# 4. Lightpanda headless browser
crau archive archive.har https://example.com --backend lightpanda --format rendered-only-har
```

Available formats:
- `warc`: standard ISO WARC archive format. With the `http` backend the records
  contain the bytes exactly as received (chunked framing included), plus
  `warcinfo`, `WARC-IP-Address` and `WARC-Concurrent-To` metadata.
- `har`: complete wire-level HTTP Archive (all network transactions).
- `rendered-har`: all network transactions, with target pages replaced by the JavaScript-rendered DOM.
- `rendered-only-har`: only target navigation pages containing their JavaScript-rendered DOM (no secondary assets).

### Installing Browser Dependencies

Browser engines are optional. `crau browser install chromium` (or `firefox`, `lightpanda`,
`all`) installs them the right way for the distribution: apt on Debian, Chrome for Testing
on Ubuntu (whose apt packages are snap stubs), Mozilla's APT repository for Firefox, apk on
Alpine, or downloads into `~/.local` without root (`-n` prints the plan). `crau browser
doctor` shows what was found.

- **Custom Profile**: pass `--user-data-dir /path/to/profile` to persist cookies and sessions.

The browser backends are built on `crau.browser` (see "Browser automation" below): browsers
are started as plain processes on a port they pick themselves (no automation flags, writable
`HOME`, no core dumps, `HeadlessChrome` hidden with a launch flag so the client hints stay
coherent) and driven over CDP or WebDriver BiDi with `navigator.webdriver === false`. Every
redirect hop, the headers really sent (cookies included), request bodies and response
bodies are recorded.

Everything the browser requests while showing a page is archived, not only what is
in the HTML: after the load event crau keeps capturing for up to `--browser-settle`
seconds (default 10), scrolling the page one viewport at a time so lazy images,
`IntersectionObserver` content and infinite scroll are triggered, and stops once at
least 2 seconds have passed and the network has been idle for half a second. XHR/fetch
calls fired by timers or by scrolling end up in the WARC/HAR with their real status,
headers and body; the same URLs are not fetched again by the link extractor. Use
`--no-auto-scroll` to only wait, or `--browser-settle 0` to collect right after load.
WebSocket and Server-Sent Events traffic is not captured, nor anything that needs a
click (for that, drive the page with `crau.browser` and record it yourself). Firefox bodies
come from BiDi network data collectors, which Firefox ESR 140 does not have: use a current
Firefox. Lightpanda does not load images.

### Extracting data from an archive

List archived URLs in a WARC file:

```bash
crau list myarchive.warc.gz
```

Extract a file from an archive:

```bash
crau extract myarchive.warc.gz https://example.com/page.html extracted-page.html
```

### Playing the archived data on your Web browser

Run a server on [localhost:8000](http://localhost:8000) to play your archive (see `-p`/`-b`;
needs `pip install crau[play]`):

```bash
crau play myarchive.warc.gz
```

### Packing downloaded files into a WARC

If you've mirrored a website using `wget -r`, `httrack` or a similiar tool in
which you have the files in your file system, you can use `crau` to create a
WARC file based on this. Run:

```bash
crau pack [--inner-directory=path] <start-url> <path-or-archive> <warc-filename>
```

Where:

- `start-url`: base URL you've downloaded (this will be joined with the
  actual file names to create the complete URL).
- `path-or-archive`: path where the files are located. Can also be a
  `.tar.gz`, `.tar.bz2`, `.tar.xz` or `.zip` archive. `crau` will retrieve all
  files recursively.
- `warc-filename`: file to be created.
- `--inner-directory`: used when a TAR/ZIP archive is passed to filter which
  directory inside the archive will be used to retrieve files. Example: you
  have an archive with a `backup/` directory on the root and a
  `www.example.com/` inside of it, so the files are actually inside
  `backup/www.example.com/` - just pass
  `--inner-directory=backup/www.example.com/` and only the files inside this
  path will be considered (in this example, the file
  `backup/www.example.com/contact.html` will be archived as
  `<start-url>/contact.html`).


## Browser automation

The same browser layer is available on its own, with no dependency besides the standard
library (`pip install crau` is enough):

```bash
crau page text https://example.com                     # rendered text; also html, js, outline, cookies
crau page screenshot https://example.com shot.png -f   # full-page PNG; `crau page pdf` for PDF
crau har capture https://example.com out.har           # everything the page requested, unfiltered
crau har summary out.har -M json                       # endpoints and the shape of their bodies
crau browser start work && crau tab open work https://example.com/login
crau page js tab:1 "document.title" -i work && crau browser stop work
crau search "open data brazil"
```

```python
import asyncio
from crau.browser import Browser

async def main():
    async with Browser("firefox") as browser:
        tab = await browser.new_tab("https://example.com", wait_selector="h1")
        await tab.click("text=More information")
        print(await tab.evaluate("document.title"))

asyncio.run(main())
```

`crau skill` prints an agent skill (a `SKILL.md`) that teaches an AI agent to use all of this;
`crau skill -l` lists its reference documents and `crau skill -r NAME` prints one. Complete
scripts live in `examples/`.

## Using crau as a library

The same engine can scrape structured data. Callbacks receive a `Response`
(`.url`, `.status`, `.text`, `.body`, `.headers`, `.xpath()`, `.urljoin()`) and may
yield or return `Request` objects and items (dicts or dataclasses), which the
`ItemPipeline` appends to `items.jsonl`/`<Class>.jsonl` (or `.csv`):

```python
from crau import Crawler, Request, Response, Spider
from crau.pipeline import ItemPipeline


class NoticiasSpider(Spider):
    start_urls = ["https://example.com/noticias"]
    allowed_uris = ["example.com"]
    max_depth = 2

    async def parse(self, response: Response):
        for titulo in response.xpath("//h2/text()"):
            yield {"url": response.url, "titulo": titulo}
        for link in response.xpath("//a[@class='next']/@href"):
            yield Request(response.urljoin(link))


Crawler.from_spider(NoticiasSpider(), pipeline=ItemPipeline("output/")).run()
```

Errors are reported through the `crau` logger (`logging.getLogger("crau")`), and
`Crawler.stats` holds `fetched`/`cached`/`failed` counters after `run()`.


## Why not X?

There are other archiving tools, of course. The motivation to start this
project was a lack of easy, fast and robust software to archive URLs - I just
wanted to execute one command without thinking and get a WARC file. Depending
on your problem, crau may not be the best answer - check out more archiving
tools in
[awesome-web-archiving](https://github.com/iipc/awesome-web-archiving#acquisition).

### Why not [GNU Wget](https://www.gnu.org/software/wget/)?

- Lacks parallel downloading;
- Some versions just crashes with segmentation fault depending on the website;
- Lots of options make the task of archiving difficult;
- There's no easy way to extend its behavior.

### Why not [Wpull](https://wpull.readthedocs.io/en/master/)?

- Lots of options make the task of archiving difficult;
- Wpull is largely unmaintained and difficult to extend. crau provides a modern,
  extensible async Python engine with pluggable fetchers (raw HTTP, Lightpanda, CDP).

### Why not [crawl]()?

- Lacks some features and it's difficult to contribute to (the [Gitlab instance
  where it's hosted](https://git.autistici.org/ale/crawl) doesn't allow
  registration);
- Has some bugs regarding to collecting page dependencies (like static assets
  inside a CSS file);
- Has a bug where it enters in a loop (if a static asset returns a HTML page
  instead of the expected file it ignores depth and keep trying to get this
  page's dependencies - if any of the latter dependencies also has the same
  problem it keeps going on infinite depth).

### Why not [archivenow](https://github.com/oduwsdl/archivenow)?

This tool can be used easily to use archiving services such as
[archive.is](https://archive.is) via command-line and can also, but when
archiving it calls wget to do the job.


## Contributing

Clone the repository:

```bash
git clone https://github.com/turicas/crau.git
```

Install development dependencies (you may want to create a virtualenv):

```bash
cd crau && pip install -r requirements-development.txt
```

Run the test suite (unit tests plus end-to-end runs against local test sites) and
the linter:

```bash
pytest
ruff check --select F crau tests
```

Tests that drive real browsers are skipped when a browser is not installed (`REQUIRE_BROWSERS=1`
turns that into a failure). `make build && make start && make test` runs everything in a
container that has the three browsers; `TESTING.md` has the details and `AGENTS.md` the
conventions of the browser package.

`scripts/smoke_test.py` runs the archiver against a real site with every backend
that is installed (`--backend all`).

Install an editable version of the package:

```bash
pip install -e .
```

Modify everything you want to, commit to another branch and then create a pull
request at GitHub.
