from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import lxml.html
from warcio.warcwriter import WARCWriter

from crau.cache.base import CacheBackend
from crau.cache.sqlite import SqliteCacheBackend
from crau.engine.scheduler import Scheduler
from crau.engine.throttle import AutoThrottle
from crau.extractor import extract_css_urls, extract_resources
from crau.fetchers.base import Fetcher
from crau.fetchers.cdp import CdpBrowserFetcher
from crau.fetchers.chromium import ChromiumFetcher
from crau.fetchers.firefox import FirefoxFetcher
from crau.fetchers.http import AsyncHttpFetcher
from crau.fetchers.lightpanda import LightpandaFetcher
from crau.models import NetworkTransaction, Page, create_har_log, create_warcinfo_record
from crau.pipeline import ItemPipeline
from crau.utils import decode_body, resource_matches_base_url
from crau.version import __version__

logger = logging.getLogger("crau")


@dataclass
class Request:
    url: str
    callback: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    url: str
    status: int
    text: str
    body: bytes
    headers: list[tuple[bytes, bytes]]
    transactions: list[NetworkTransaction]
    meta: dict[str, Any] = field(default_factory=dict)

    def urljoin(self, url: str) -> str:
        return urljoin(self.url, url)

    def xpath(self, query: str) -> list[str]:
        if not self.text:
            return []
        try:
            tree = lxml.html.fromstring(self.text)
            results = tree.xpath(query)
            return [str(r) for r in results]
        except Exception:
            return []


class Spider:
    """Base class for object-oriented scrapers; run one with ``Crawler.from_spider(spider).run()``.

    ``parse`` receives every response (start URLs and Requests yielded without an
    explicit callback) and may yield/return Requests and items.
    """

    start_urls: list[str] = []
    allowed_uris: list[str] = []
    max_depth: int = 1

    async def parse(self, response: Response):
        pass


class Crawler:
    """Unified asynchronous crawler engine for archiving and scraping."""

    def __init__(
        self,
        start_urls: list[str],
        max_depth: int = 1,
        allowed_uris: list[str] | None = None,
        backend: str = "http",
        fetcher: Fetcher | None = None,
        cache: CacheBackend | None = None,
        cache_type: str = "none",
        cache_path: str | Path = ".crau_cache.sqlite",
        warc_filename: str | Path | None = None,
        har_filename: str | Path | None = None,
        format: str = "warc",
        pipeline: ItemPipeline | None = None,
        concurrency: int = 16,
        concurrency_per_domain: int = 4,
        autothrottle: bool = True,
        user_agent: str | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        binary_path: str | Path | None = None,
        user_data_dir: str | Path | None = None,
        progress: Callable[[dict[str, int]], Any] | None = None,
        browser_settle: float = 10.0,
        browser_auto_scroll: bool = True,
    ):
        self.start_urls = list(start_urls)
        self.max_depth = max_depth
        self.allowed_uris = allowed_uris or []
        self.backend = backend.lower()
        self.fetcher = fetcher
        self.cache = cache
        self.cache_type = cache_type.lower()
        self.cache_path = Path(cache_path)
        self.warc_filename = Path(warc_filename) if warc_filename else None
        self.har_filename = Path(har_filename) if har_filename else None
        self.format = format.lower()
        self.pipeline = pipeline
        self.concurrency = concurrency
        self.concurrency_per_domain = concurrency_per_domain
        self.autothrottle = autothrottle
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.binary_path = binary_path
        self.user_data_dir = user_data_dir

        self.browser_settle = browser_settle
        self.browser_auto_scroll = browser_auto_scroll
        self.progress = progress
        self.stats: dict[str, int] = {"fetched": 0, "cached": 0, "failed": 0, "pending": 0}

        self._default_callback: Callable[[Response], Any] | None = None
        self._all_transactions: list[NetworkTransaction] = []
        self._pages: list[Page] = []

    @classmethod
    def from_spider(cls, spider: Spider, **kwargs: Any) -> "Crawler":
        """Build a crawler from a Spider: its start_urls, allowed_uris, max_depth and parse callback.

        Keyword arguments override the spider attributes and are passed to Crawler().
        """
        options: dict[str, Any] = {
            "start_urls": list(spider.start_urls),
            "allowed_uris": list(spider.allowed_uris),
            "max_depth": spider.max_depth,
        }
        options.update(kwargs)
        crawler = cls(**options)
        crawler.on_response(spider.parse)
        return crawler

    def on_response(self, fn: Callable[[Response], Any]):
        """Decorator for setting default response parser callback."""
        self._default_callback = fn
        return fn

    def _resolve_fetcher(self) -> Fetcher:
        if self.fetcher:
            return self.fetcher
        browser_options = {
            "timeout": self.timeout,
            "user_agent": self.user_agent,
            "settle_timeout": self.browser_settle,
            "auto_scroll": self.browser_auto_scroll,
        }
        if self.backend == "chromium":
            return ChromiumFetcher(binary_path=self.binary_path, user_data_dir=self.user_data_dir, **browser_options)
        elif self.backend == "firefox":
            return FirefoxFetcher(binary_path=self.binary_path, user_data_dir=self.user_data_dir, **browser_options)
        elif self.backend == "lightpanda":
            return LightpandaFetcher(
                binary_path=str(self.binary_path) if self.binary_path else "lightpanda", **browser_options
            )
        elif self.backend == "cdp":
            return CdpBrowserFetcher(endpoint_url="ws://127.0.0.1:9222", **browser_options)
        else:
            return AsyncHttpFetcher(
                user_agent=self.user_agent,
                timeout=self.timeout,
            )

    async def _handle_callback_results(
        self,
        results: Any,
        current_depth: int,
        scheduler: Scheduler,
    ) -> None:
        if results is None:
            return

        if inspect.isasyncgen(results):
            async for item in results:
                await self._process_yielded_item(item, current_depth, scheduler)
        elif inspect.isgenerator(results) or isinstance(results, (list, tuple)):
            for item in results:
                await self._process_yielded_item(item, current_depth, scheduler)
        else:
            # a callback may return a single Request or item instead of yielding
            await self._process_yielded_item(results, current_depth, scheduler)

    async def _process_yielded_item(
        self,
        item: Any,
        current_depth: int,
        scheduler: Scheduler,
    ) -> None:
        if isinstance(item, Request):
            await scheduler.enqueue(
                url=item.url,
                depth=current_depth + 1,
                callback=item.callback or self._default_callback,
                meta=item.meta,
            )
        elif isinstance(item, dict) or (dataclasses.is_dataclass(item) and not isinstance(item, type)):
            if self.pipeline:
                self.pipeline.process_item(item)

    async def _worker(self, fetcher, scheduler, throttle, cache, warc_writer, warc_fobj) -> None:
        while True:
            item = await scheduler.dequeue()
            try:
                await self._process_item(item, fetcher, scheduler, throttle, cache, warc_writer, warc_fobj)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["failed"] += 1
                logger.exception("Unexpected error while processing %s", item.url)
            finally:
                scheduler.task_done()
                self._report_progress(scheduler)

    def _report_progress(self, scheduler: Scheduler) -> None:
        self.stats["pending"] = scheduler.qsize()
        if self.progress is not None:
            self.progress(dict(self.stats))

    async def _fetch_with_retries(self, fetcher, url: str) -> tuple[list[NetworkTransaction], Page | None]:
        attempt = 0
        while True:
            try:
                result = await fetcher.fetch(url)
                return result.transactions, result.page
            except asyncio.CancelledError:
                raise
            except Exception as error:
                attempt += 1
                if attempt > self.max_retries:
                    logger.warning(
                        "Giving up on %s after %d attempt(s): %s: %s",
                        url,
                        attempt,
                        type(error).__name__,
                        error,
                    )
                    return [], None
                delay = 0.5 * (2**attempt)
                logger.info(
                    "Retrying %s in %.1fs (attempt %d/%d): %s: %s",
                    url,
                    delay,
                    attempt,
                    self.max_retries,
                    type(error).__name__,
                    error,
                )
                await asyncio.sleep(delay)

    async def _process_item(self, item, fetcher, scheduler, throttle, cache, warc_writer, warc_fobj) -> None:
        domain = urlparse(item.url).netloc

        async with scheduler.limit(domain):
            await throttle.wait_throttle(domain)

            # 1. Check cache
            transactions = None
            if cache is not None:
                transactions = await cache.get(item.url)
                if transactions:
                    self.stats["cached"] += 1
                    logger.debug("Cache hit for %s", item.url)

            # 2. Fetch from network on cache miss
            page = None
            if not transactions:
                transactions, page = await self._fetch_with_retries(fetcher, item.url)
                if not transactions:
                    self.stats["failed"] += 1
                    return
                self.stats["fetched"] += 1
                throttle.record_latency(domain, transactions[-1].duration_seconds)
                if cache is not None:
                    await cache.store(item.url, transactions)

        # From here on the domain slot is released: parsing does not hit the network
        self._all_transactions.extend(transactions)
        # Everything the fetcher captured while loading this page (sub-resources loaded by a
        # browser, redirect hops) is archived already: never fetch it again
        for tx in transactions:
            scheduler.mark_seen(tx.request.url)

        # 3. Write to WARC (synchronous block: records of one transaction are never interleaved)
        if warc_writer is not None:
            for tx in transactions:
                req_rec, resp_rec = tx.to_warc_records(warc_writer)
                warc_writer.write_record(resp_rec)
                warc_writer.write_record(req_rec)
            if warc_fobj is not None:
                warc_fobj.flush()

        is_dependency = item.meta.get("is_dependency", False)
        if page is None:
            # cache hit or plain HTTP: the document is the last hop of the redirect chain
            final_tx = transactions[-1]
            page = Page(
                url=final_tx.request.url,
                status_code=final_tx.response.status_code,
                content=decode_body(final_tx.response.body, final_tx.response.raw_headers),
                raw_body=final_tx.response.body,
                transactions=transactions,
                main_transaction=final_tx,
            )
        if not is_dependency:
            self._pages.append(page)

        # The document transaction: for browsers the last transaction is usually an asset.
        # Response.text is the rendered DOM (what links are extracted from); Response.body
        # stays the bytes that came over the wire
        final_tx = page.get_main_transaction() or transactions[-1]
        resp = Response(
            url=final_tx.request.url,
            status=final_tx.response.status_code,
            text=page.content,
            body=final_tx.response.body,
            headers=final_tx.response.raw_headers,
            transactions=transactions,
            meta=item.meta,
        )

        # 4. Process response via callback or default crawler link extraction
        callback = item.callback or self._default_callback
        if callback is not None:
            result = callback(resp)
            if inspect.iscoroutine(result):
                result = await result
            await self._handle_callback_results(result, item.depth, scheduler)
            return

        content_type = ""
        for k, v in final_tx.response.raw_headers:
            if k.lower() == b"content-type":
                content_type = v.decode("latin1").lower()
                break

        is_css = "text/css" in content_type or (not content_type and urlparse(item.url).path.lower().endswith(".css"))
        if is_css:
            # url() references inside stylesheets (fonts, background images) are needed to
            # render the page; they stay at the same depth
            for css_url in extract_css_urls(item.url, resp.text):
                await scheduler.enqueue(css_url, depth=item.depth, callback=None, meta={"is_dependency": True})
        elif "text/html" in content_type or not content_type:
            for res in extract_resources(item.url, resp.text):
                if res.link_type == "dependency":
                    # Dependencies stay at the same depth without allowed_uris restrictions
                    await scheduler.enqueue(res.url, depth=item.depth, callback=None, meta={"is_dependency": True})
                elif res.link_type == "anchor" and item.depth < self.max_depth:
                    if resource_matches_base_url(res.url, self.allowed_uris):
                        await scheduler.enqueue(
                            res.url, depth=item.depth + 1, callback=None, meta={"is_dependency": False}
                        )

    async def crawl(self) -> None:
        fetcher = self._resolve_fetcher()
        scheduler = Scheduler(
            max_concurrent_requests=self.concurrency,
            max_concurrent_per_domain=self.concurrency_per_domain,
        )
        throttle = AutoThrottle(enabled=self.autothrottle)

        # Cache setup: an explicit backend wins over cache_type; backends are opened here
        # so callers do not need to enter them (entering twice is a no-op)
        cache = self.cache
        if cache is None and self.cache_type == "sqlite":
            cache = SqliteCacheBackend(self.cache_path)
        elif cache is None and self.cache_type not in ("none", ""):
            raise ValueError(f"Unknown cache_type: {self.cache_type!r} (expected 'sqlite' or 'none')")
        if cache is not None:
            await cache.__aenter__()

        # WARC Writer setup
        warc_fobj = None
        warc_writer = None
        if self.warc_filename:
            self.warc_filename.parent.mkdir(parents=True, exist_ok=True)
            gzip = self.warc_filename.name.endswith(".gz")
            warc_fobj = open(self.warc_filename, mode="wb")
            warc_writer = WARCWriter(warc_fobj, gzip=gzip)
            warc_writer.write_record(create_warcinfo_record(warc_writer, self.warc_filename.name, __version__))

        # Enqueue initial URLs
        for url in self.start_urls:
            await scheduler.enqueue(
                url=url,
                depth=0,
                callback=self._default_callback,
            )

        async with fetcher:
            workers = [
                asyncio.create_task(
                    self._worker(fetcher, scheduler, throttle, cache, warc_writer, warc_fobj),
                    name=f"crau-worker-{number}",
                )
                for number in range(max(1, self.concurrency))
            ]
            try:
                await scheduler.join()
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        # Teardown
        if warc_fobj is not None:
            warc_fobj.close()

        if self.har_filename:
            self.har_filename.parent.mkdir(parents=True, exist_ok=True)
            mode = "raw"
            if self.format == "rendered-har":
                mode = "rendered"
            elif self.format == "rendered-only-har":
                mode = "rendered-only"

            har_data = create_har_log(
                self._all_transactions,
                pages=self._pages,
                mode=mode,
                creator_version=__version__,
            )
            self.har_filename.write_text(
                json.dumps(har_data, indent=2), encoding="utf-8"
            )

        if self.pipeline is not None:
            self.pipeline.close()

        if cache is not None:
            await cache.__aexit__(None, None, None)

    def run(self) -> None:
        """Synchronous entrypoint to run crawler."""
        asyncio.run(self.crawl())
