from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import time
from typing import Any

from crau.fetchers.base import Fetcher, NetworkActivity, settle_page
from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

logger = logging.getLogger("crau")

HEADLESS_UA_TOKEN = "HeadlessChrome"


def _headers_to_tuples(headers: dict[str, Any] | None) -> list[tuple[bytes, bytes]]:
    return [
        (key.encode("latin1"), str(value).encode("latin1", errors="replace"))
        for key, value in (headers or {}).items()
    ]


def _http_version(protocol: str | None) -> str:
    """CDP reports 'http/1.1', 'h2', 'h3'; normalize to the HTTP version notation used in WARC."""
    if not protocol:
        return "HTTP/1.1"
    protocol = protocol.lower()
    if protocol == "h2":
        return "HTTP/2"
    if protocol == "h3":
        return "HTTP/3"
    return protocol.upper()


class CdpBrowserFetcher(Fetcher):
    """Generic browser fetcher speaking the Chrome DevTools Protocol over a raw websocket.

    ``endpoint_url`` is the browser-level websocket (the ``webSocketDebuggerUrl`` of
    ``/json/version``). Page-level domains (Page, Network, Runtime) are not available
    there, so on enter the fetcher attaches to a page target (``Target.attachToTarget``
    with ``flatten``) and sends every page command with that ``sessionId``. A single
    reader task dispatches command replies by id and buffers session events.

    After the load event the fetcher keeps listening for ``settle_timeout`` seconds at
    most, scrolling the page to trigger lazy content, and collects the traffic once at
    least ``min_settle_time`` has passed and the network has been idle for
    ``idle_time``; everything the page requested in that window becomes a transaction.

    A fetcher drives one tab, so ``fetch`` calls are serialized.
    """

    def __init__(
        self,
        endpoint_url: str,
        timeout: float = 30.0,
        user_agent: str | None = None,
        settle_timeout: float = 10.0,
        idle_time: float = 0.5,
        min_settle_time: float = 2.0,
        auto_scroll: bool = True,
    ):
        if websockets is None:
            raise ImportError(
                "The 'websockets' library is required for CDP browser fetchers. "
                "Install it via 'pip install crau[browser]'."
            )
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self.user_agent = user_agent
        self.settle_timeout = settle_timeout
        self.idle_time = idle_time
        self.min_settle_time = min_settle_time
        self.auto_scroll = auto_scroll
        self._activity = NetworkActivity()
        self._ws: Any = None
        self._msg_id = 0
        self._session_id: str | None = None
        self._target_id: str | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[dict[str, Any]] = []
        self._event_waiters: list[tuple[str, asyncio.Future]] = []
        self._reader_task: asyncio.Task | None = None
        self._fetch_lock = asyncio.Lock()

    # --- connection -------------------------------------------------------------

    async def __aenter__(self) -> "CdpBrowserFetcher":
        self._ws = await websockets.connect(self.endpoint_url, max_size=None)
        self._reader_task = asyncio.create_task(self._reader_loop(), name="crau-cdp-reader")
        try:
            await self._attach_to_page()
            await self.send("Page.enable")
            # bigger buffers: Chrome evicts response bodies beyond the defaults (~10 MB)
            await self.send(
                "Network.enable",
                {"maxTotalBufferSize": 256 * 1024 * 1024, "maxResourceBufferSize": 64 * 1024 * 1024},
            )
            await self.send("Runtime.enable")
            if self.user_agent:
                await self.send("Network.setUserAgentOverride", {"userAgent": self.user_agent})
        except Exception:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._session_id = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _attach_to_page(self) -> None:
        """Pick an existing unattached page target (or create one) and attach with a flat session."""
        target_id = None
        result = await self.send("Target.getTargets", session=False)
        for info in result.get("targetInfos", []):
            if info.get("type") == "page" and not info.get("attached"):
                target_id = info["targetId"]
                break
        if target_id is None:
            created = await self.send("Target.createTarget", {"url": "about:blank"}, session=False)
            target_id = created["targetId"]
        attached = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}, session=False)
        self._target_id = target_id
        self._session_id = attached["sessionId"]

    async def _reader_loop(self) -> None:
        async for raw_message in self._ws:
            data = json.loads(raw_message)
            message_id = data.get("id")
            if message_id is not None:
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    if "error" in data:
                        error = data["error"]
                        future.set_exception(RuntimeError(f"CDP error {error.get('code')}: {error.get('message')}"))
                    else:
                        future.set_result(data.get("result", {}))
                continue
            if "method" in data:
                self._events.append(data)
                request_id = data.get("params", {}).get("requestId")
                if request_id:
                    if data["method"] == "Network.requestWillBeSent":
                        self._activity.started(request_id)
                    elif data["method"] in ("Network.loadingFinished", "Network.loadingFailed"):
                        self._activity.finished(request_id)
                for method, future in list(self._event_waiters):
                    if data["method"] == method and not future.done():
                        future.set_result(data)

    async def send(self, method: str, params: dict[str, Any] | None = None, session: bool = True) -> dict[str, Any]:
        """Send a command and wait for its reply; page commands carry the attached sessionId."""
        if self._ws is None:
            raise RuntimeError("Fetcher connection is not open. Use 'async with fetcher:'")
        self._msg_id += 1
        payload: dict[str, Any] = {"id": self._msg_id, "method": method, "params": params or {}}
        if session and self._session_id:
            payload["sessionId"] = self._session_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._msg_id] = future
        await self._ws.send(json.dumps(payload))
        return await asyncio.wait_for(future, timeout=self.timeout)

    async def wait_for_event(self, method: str, timeout: float | None = None) -> dict[str, Any]:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._event_waiters.append((method, future))
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.timeout)
        finally:
            self._event_waiters.remove((method, future))

    # --- fetching ---------------------------------------------------------------

    async def fetch(self, url: str, **kwargs: Any) -> CrawlResult:
        """Navigate to url and capture network traffic and the rendered DOM (one page at a time)."""
        async with self._fetch_lock:
            return await self._fetch_unlocked(url, **kwargs)

    async def _fetch_unlocked(
        self,
        url: str,
        evaluate_js: str | None = None,
        **kwargs: Any,
    ) -> CrawlResult:
        if not self._session_id:
            raise RuntimeError("Fetcher is not attached to a page. Use 'async with fetcher:'")

        start_time = time.perf_counter()
        self._events.clear()
        self._activity.reset()

        load_waiter = asyncio.ensure_future(self.wait_for_event("Page.loadEventFired"))
        try:
            navigation = await self.send("Page.navigate", {"url": url})
            if navigation.get("errorText"):
                raise RuntimeError(f"Navigation to {url} failed: {navigation['errorText']}")
            await load_waiter
        except BaseException:
            load_waiter.cancel()
            raise

        settle = {}
        if self.settle_timeout > 0:
            settle = await settle_page(
                self.evaluate,
                self._activity,
                timeout=self.settle_timeout,
                idle=self.idle_time,
                min_time=self.min_settle_time,
                auto_scroll=self.auto_scroll,
            )
            if settle["pending"]:
                logger.debug("%s: %d request(s) still in flight after settle timeout", url, settle["pending"])

        if evaluate_js:
            await self.evaluate(evaluate_js)

        rendered_html = await self.evaluate("document.documentElement.outerHTML") or ""

        transactions = await self._collect_transactions(list(self._events))
        finished_at = time.perf_counter()

        main_tx = None
        for tx in transactions:
            if tx.request.url == url:
                main_tx = tx
                break
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

    async def evaluate(self, expression: str) -> Any:
        """Evaluate a JS expression in the page and return its value."""
        result = await self.send(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        )
        return result.get("result", {}).get("value")

    async def _collect_transactions(self, events: list[dict[str, Any]]) -> list[NetworkTransaction]:
        """Pair requestWillBeSent/responseReceived events and download each response body.

        Requests that never got a response (aborted, loadingFailed) are skipped rather
        than recorded with an invented status.
        """
        requests: dict[str, dict[str, Any]] = {}
        responses: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in events:
            params = event.get("params", {})
            request_id = params.get("requestId")
            if not request_id:
                continue
            if event["method"] == "Network.requestWillBeSent":
                if request_id not in requests:
                    order.append(request_id)
                requests[request_id] = params
            elif event["method"] == "Network.responseReceived":
                responses[request_id] = params

        transactions: list[NetworkTransaction] = []
        for request_id in order:
            request_params = requests[request_id]
            response_params = responses.get(request_id)
            if response_params is None:
                logger.debug("Skipping request without response: %s", request_params.get("request", {}).get("url"))
                continue
            request = request_params.get("request", {})
            response = response_params.get("response", {})

            body_bytes = b""
            try:
                body_result = await self.send("Network.getResponseBody", {"requestId": request_id})
                raw = body_result.get("body", "")
                body_bytes = base64.b64decode(raw) if body_result.get("base64Encoded") else raw.encode("utf-8")
            except Exception as error:
                logger.debug("No body for %s: %s", request.get("url"), error)

            wall_time = request_params.get("wallTime")
            timestamp = (
                datetime.datetime.fromtimestamp(wall_time, datetime.timezone.utc)
                if wall_time
                else datetime.datetime.now(datetime.timezone.utc)
            )
            raw_request = RawRequest(
                url=request.get("url", ""),
                method=request.get("method", "GET"),
                raw_headers=_headers_to_tuples(request.get("headers")),
                raw_body=(request.get("postData") or "").encode("utf-8"),
                timestamp=timestamp,
            )
            remote_address = None
            if response.get("remoteIPAddress"):
                remote_address = (response["remoteIPAddress"], int(response.get("remotePort") or 0))
            raw_response = RawResponse(
                status_code=int(response.get("status", 0)),
                reason_phrase=response.get("statusText") or "",
                http_version=_http_version(response.get("protocol")),
                raw_headers=_headers_to_tuples(response.get("headers")),
                raw_body=body_bytes,
                timestamp=timestamp,
            )
            duration = None
            timing = response.get("timing") or {}
            if timing.get("receiveHeadersEnd") is not None:
                duration = timing["receiveHeadersEnd"] / 1000.0
            transactions.append(
                NetworkTransaction(
                    request=raw_request,
                    response=raw_response,
                    duration_seconds=duration,
                    remote_address=remote_address,
                )
            )
        return transactions
