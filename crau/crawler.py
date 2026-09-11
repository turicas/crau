from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import urljoin, urlparse

import lxml.html
from warcio.warcwriter import WARCWriter

from crau.cache.base import CacheBackend
from crau.cache.sqlite import SqliteCacheBackend
from crau.engine.scheduler import RequestItem, Scheduler
from crau.engine.throttle import AutoThrottle
from crau.extractor import extract_resources
from crau.fetchers.base import Fetcher
from crau.fetchers.cdp import CdpBrowserFetcher
from crau.fetchers.http import AsyncHttpFetcher
from crau.fetchers.lightpanda import LightpandaFetcher
from crau.models import NetworkTransaction, create_har_log
from crau.pipeline import ItemPipeline
from crau.utils import resource_matches_base_url
from crau.version import __version__


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
    """Base class for object-oriented scrapers."""

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
        cache_type: str = "sqlite",
        warc_filename: str | Path | None = None,
        har_filename: str | Path | None = None,
        pipeline: ItemPipeline | None = None,
        concurrency: int = 16,
        concurrency_per_domain: int = 4,
        autothrottle: bool = True,
        user_agent: str | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.start_urls = list(start_urls)
        self.max_depth = max_depth
        self.allowed_uris = allowed_uris or []
        self.backend = backend.lower()
        self.fetcher = fetcher
        self.cache = cache
        self.cache_type = cache_type.lower()
        self.warc_filename = Path(warc_filename) if warc_filename else None
        self.har_filename = Path(har_filename) if har_filename else None
        self.pipeline = pipeline
        self.concurrency = concurrency
        self.concurrency_per_domain = concurrency_per_domain
        self.autothrottle = autothrottle
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries

        self._default_callback: Callable[[Response], Any] | None = None
        self._all_transactions: list[NetworkTransaction] = []

    def on_response(self, fn: Callable[[Response], Any]):
        """Decorator for setting default response parser callback."""
        self._default_callback = fn
        return fn

    def _resolve_fetcher(self) -> Fetcher:
        if self.fetcher:
            return self.fetcher
        if self.backend == "lightpanda":
            return LightpandaFetcher(timeout=self.timeout, user_agent=self.user_agent)
        elif self.backend == "cdp":
            return CdpBrowserFetcher(
                endpoint_url="ws://127.0.0.1:9222",
                timeout=self.timeout,
                user_agent=self.user_agent,
            )
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

    async def crawl(self) -> None:
        fetcher = self._resolve_fetcher()
        scheduler = Scheduler(
            max_concurrent_requests=self.concurrency,
            max_concurrent_per_domain=self.concurrency_per_domain,
        )
        throttle = AutoThrottle(enabled=self.autothrottle)

        # Cache setup
        cache = self.cache
        should_close_cache = False
        if cache is None and self.cache_type == "sqlite":
            cache = SqliteCacheBackend()
            should_close_cache = True
            await cache.__aenter__()

        # WARC Writer setup
        warc_fobj = None
        warc_writer = None
        if self.warc_filename:
            self.warc_filename.parent.mkdir(parents=True, exist_ok=True)
            gzip = self.warc_filename.name.endswith(".gz")
            warc_fobj = open(self.warc_filename, mode="wb")
            warc_writer = WARCWriter(warc_fobj, gzip=gzip)

        # Enqueue initial URLs
        for url in self.start_urls:
            await scheduler.enqueue(
                url=url,
                depth=0,
                callback=self._default_callback,
            )

        async with fetcher:
            while not scheduler.empty():
                item = await scheduler.dequeue()
                domain = urlparse(item.url).netloc

                async with scheduler.limit(domain):
                    await throttle.wait_throttle(domain)

                    # 1. Check cache
                    transactions = None
                    if cache is not None:
                        transactions = await cache.get(item.url)

                    # 2. Fetch from network on cache miss
                    if not transactions:
                        retries = 0
                        while retries <= self.max_retries:
                            try:
                                res = await fetcher.fetch(item.url)
                                transactions = res.transactions
                                if transactions:
                                    last_tx = transactions[-1]
                                    throttle.record_latency(
                                        domain, last_tx.duration_seconds
                                    )
                                    if cache is not None:
                                        await cache.store(item.url, transactions)
                                break
                            except Exception:
                                retries += 1
                                if retries > self.max_retries:
                                    transactions = []
                                    break
                                await asyncio.sleep(0.5 * (2**retries))

                    if not transactions:
                        scheduler.task_done()
                        continue

                    self._all_transactions.extend(transactions)

                    # 3. Write to WARC
                    if warc_writer is not None:
                        for tx in transactions:
                            req_rec, resp_rec = tx.to_warc_records(warc_writer)
                            warc_writer.write_record(req_rec)
                            warc_writer.write_record(resp_rec)
                        if warc_fobj is not None:
                            warc_fobj.flush()

                    final_tx = transactions[-1]
                    resp = Response(
                        url=final_tx.request.url,
                        status=final_tx.response.status_code,
                        text=final_tx.response.raw_body.decode("utf-8", errors="replace"),
                        body=final_tx.response.raw_body,
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
                        await self._handle_callback_results(
                            result, item.depth, scheduler
                        )
                    else:
                        # Default archiver: extract media dependencies and anchor links
                        content_type = ""
                        for k, v in final_tx.response.raw_headers:
                            if k.lower() == b"content-type":
                                content_type = v.decode("latin1").lower()
                                break

                        if "text/html" in content_type or not content_type:
                            resources = extract_resources(item.url, resp.text)
                            for res in resources:
                                if res.link_type == "dependency":
                                    # Dependencies stay at the same depth without allowed_uris restrictions
                                    await scheduler.enqueue(
                                        res.url,
                                        depth=item.depth,
                                        callback=None,
                                    )
                                elif res.link_type == "anchor":
                                    if item.depth < self.max_depth:
                                        if resource_matches_base_url(
                                            res.url, self.allowed_uris
                                        ):
                                            await scheduler.enqueue(
                                                res.url,
                                                depth=item.depth + 1,
                                                callback=None,
                                            )

                    scheduler.task_done()

        # Teardown
        if warc_fobj is not None:
            warc_fobj.close()

        if self.har_filename:
            self.har_filename.parent.mkdir(parents=True, exist_ok=True)
            har_data = create_har_log(
                self._all_transactions,
                creator_version=__version__,
            )
            self.har_filename.write_text(
                json.dumps(har_data, indent=2), encoding="utf-8"
            )

        if self.pipeline is not None:
            self.pipeline.close()

        if should_close_cache and cache is not None:
            await cache.close()

    def run(self) -> None:
        """Synchronous entrypoint to run crawler."""
        asyncio.run(self.crawl())
