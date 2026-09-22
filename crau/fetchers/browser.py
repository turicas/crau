from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Any

from crau.browser import Browser
from crau.browser.network import NetworkRecord
from crau.fetchers.base import Fetcher
from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse

logger = logging.getLogger("crau")


def http_version(protocol: str | None) -> str:
    """Browsers report 'http/1.1', 'h2', 'h3'; normalize to the HTTP version notation used in WARC."""
    if not protocol:
        return "HTTP/1.1"
    protocol = protocol.lower()
    return {"h2": "HTTP/2", "h3": "HTTP/3"}.get(protocol, protocol.upper())


def _header_tuples(headers: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    return [(name.encode("latin1"), value.encode("latin1", errors="replace")) for name, value in headers]


def transaction_from_record(record: NetworkRecord) -> NetworkTransaction:
    """One recorded request/response pair (a single redirect hop) as a crau transaction."""
    timestamp = (
        datetime.datetime.fromtimestamp(record.started, datetime.timezone.utc)
        if record.started
        else datetime.datetime.now(datetime.timezone.utc)
    )
    request = RawRequest(
        url=record.url,
        method=record.method,
        raw_headers=_header_tuples(record.request_headers),
        raw_body=(record.post_data or "").encode("utf-8"),
        timestamp=timestamp,
    )
    response = RawResponse(
        status_code=record.status,
        reason_phrase=record.status_text,
        http_version=http_version(record.protocol),
        raw_headers=_header_tuples(record.response_headers),
        raw_body=record.body or b"",
        timestamp=timestamp,
    )
    return NetworkTransaction(
        request=request,
        response=response,
        duration_seconds=record.duration_ms / 1000.0 if record.duration_ms is not None and record.duration_ms >= 0 else None,
        remote_address=(record.remote_ip, record.remote_port) if record.remote_ip else None,
    )


class BrowserFetcher(Fetcher):
    """Fetcher that renders pages in a real browser driven by ``crau.browser`` (CDP or WebDriver BiDi).

    Either launches ``browser_name`` (``binary_path``, ``user_data_dir``, ``port``...) or, when
    ``endpoint_url`` is given, connects to a browser that is already running. One tab is used, so
    ``fetch`` calls are serialized.

    Everything the page requests becomes a transaction, every redirect hop included, with the headers
    really sent and received, request bodies and response bodies. After the load event the fetcher
    keeps recording for ``settle_timeout`` seconds at most, scrolling the page to trigger lazy content,
    and collects the traffic once at least ``min_settle_time`` has passed and the network has been
    idle for ``idle_time``. Requests that never got a response (aborted, blocked) are skipped rather
    than recorded with an invented status.
    """

    browser_name = "chromium"

    def __init__(
        self,
        binary_path: str | Path | None = None,
        user_data_dir: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
        window_size: tuple[int, int] = (1440, 900),
        timeout: float = 30.0,
        user_agent: str | None = None,
        extra_args: list[str] | None = None,
        settle_timeout: float = 10.0,
        idle_time: float = 0.5,
        min_settle_time: float = 2.0,
        auto_scroll: bool = True,
        endpoint_url: str | None = None,
        stealth: bool = True,
        insecure: bool = False,
    ):
        self.binary_path = str(binary_path) if binary_path else None
        self.user_data_dir = Path(user_data_dir).resolve() if user_data_dir else None
        self.host = host
        self.port = port
        self.window_size = window_size
        self.timeout = timeout
        self.user_agent = user_agent
        self.extra_args = extra_args or []
        self.settle_timeout = settle_timeout
        self.idle_time = idle_time
        self.min_settle_time = min_settle_time
        self.auto_scroll = auto_scroll
        self.endpoint_url = endpoint_url
        self.stealth = stealth
        self.insecure = insecure
        self.browser: Browser | None = None
        self.tab = None
        self._fetch_lock = asyncio.Lock()

    def _create_browser(self) -> Browser:
        if self.endpoint_url:
            return Browser.connect(self.endpoint_url, self.browser_name, stealth=self.stealth, timeout=self.timeout)
        return Browser(
            self.browser_name,
            binary=self.binary_path,
            profile=self.user_data_dir,
            viewport=self.window_size,
            # Lightpanda refuses --user-agent values that look like another browser; it gets a protocol override
            user_agent=None if self._overrides_user_agent_by_protocol else self.user_agent,
            extra_args=self.extra_args,
            stealth=self.stealth,
            insecure=self.insecure,
            host=self.host,
            port=self.port,
            # a cold start (binary not in the page cache) can take >10s
            timeout=max(self.timeout, 30.0),
        )

    async def __aenter__(self) -> "BrowserFetcher":
        self.browser = self._create_browser()
        try:
            await self.browser.start()
            self.browser.timeout = self.timeout
            self.tab = await self._open_tab()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    @property
    def _overrides_user_agent_by_protocol(self) -> bool:
        """True when the User-Agent cannot go in at launch: a browser somebody else started, or Lightpanda."""
        return bool(self.user_agent) and self.browser_name != "firefox" and (
            bool(self.endpoint_url) or self.browser_name == "lightpanda"
        )

    async def _open_tab(self):
        """A fresh tab; when connected to somebody else's CDP browser, reuse a page that is already open."""
        tab = None
        if self.endpoint_url and self.browser_name != "firefox":
            tabs = await self.browser.tabs()
            tab = tabs[0] if tabs else None
        if tab is None:
            tab = await self.browser.new_tab(apply_viewport=not self.endpoint_url)
        if self._overrides_user_agent_by_protocol:
            # without userAgentMetadata this empties navigator.userAgentData.brands and drops sec-ch-ua,
            # which is why launched Chromium gets its User-Agent as a command-line flag instead
            await tab.cdp("Network.setUserAgentOverride", {"userAgent": self.user_agent})
        return tab

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        browser, self.browser, self.tab = self.browser, None, None
        if browser is not None:
            await browser.close()

    async def fetch(self, url: str, **kwargs: Any) -> CrawlResult:
        """Navigate to url and capture network traffic and the rendered DOM (one page at a time)."""
        async with self._fetch_lock:
            return await self._fetch_unlocked(url, **kwargs)

    async def evaluate(self, expression: str) -> Any:
        """Evaluate a JS expression in the page and return its value."""
        return await self.tab.evaluate(expression)

    async def _fetch_unlocked(self, url: str, evaluate_js: str | None = None, **kwargs: Any) -> CrawlResult:
        if self.tab is None:
            raise RuntimeError("Fetcher is not connected to a browser. Use 'async with fetcher:'")

        start_time = time.perf_counter()
        await self.tab.start_recording()
        try:
            await self.tab.goto(url, wait_until="load", timeout=self.timeout)
            settle: dict[str, Any] = {}
            if self.settle_timeout > 0:
                settle = await self.tab.settle(
                    self.settle_timeout, idle=self.idle_time, minimum=self.min_settle_time, scroll=self.auto_scroll
                )
                if settle["pending"]:
                    logger.debug("%s: %d request(s) still in flight after settle timeout", url, settle["pending"])
            if evaluate_js:
                await self.tab.evaluate(evaluate_js)
            rendered_html = await self.tab.evaluate("document.documentElement.outerHTML") or ""
        finally:
            records = await self.tab.stop_recording()
        finished_at = time.perf_counter()

        transactions: list[NetworkTransaction] = []
        main_tx = None
        for record in records:
            if not record.status:
                logger.debug("Skipping request without response: %s", record.url)
                continue
            transaction = transaction_from_record(record)
            transactions.append(transaction)
            # the document is the last hop of the navigation (redirects produce several)
            if record.is_navigation or (main_tx is None and record.url == url):
                main_tx = transaction
        if main_tx is None and transactions:
            main_tx = transactions[0]
        if main_tx is None:
            raise RuntimeError(f"No network response captured for {url}")

        page = Page(
            url=url,
            status_code=main_tx.response.status_code,
            content=rendered_html,
            raw_body=main_tx.response.body,
            transactions=transactions,
            timing={"total": finished_at - start_time, **settle},
            main_transaction=main_tx,
        )
        return CrawlResult(transactions=transactions, page=page)
