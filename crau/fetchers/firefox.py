from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from crau.fetchers.base import Fetcher, NetworkActivity, settle_page
from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

logger = logging.getLogger("crau")

# Injected in every document before its scripts run: remote-controlled Firefox reports
# navigator.webdriver === true, the most common bot signal. The getter is redefined on the
# prototype, where the native one lives, so `navigator` gains no own property that
# Object.getOwnPropertyNames(navigator) would reveal.
WEBDRIVER_PRELOAD_SCRIPT = (
    "() => { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false, configurable: true }); }"
)


def _bidi_headers_to_tuples(headers: list[dict[str, Any]] | None) -> list[tuple[bytes, bytes]]:
    """BiDi headers are [{name, value: {type: 'string', value}}]."""
    result = []
    for header in headers or []:
        value = header.get("value", {})
        text = value.get("value", "") if isinstance(value, dict) else str(value)
        result.append((str(header.get("name", "")).encode("latin1"), str(text).encode("latin1", errors="replace")))
    return result


def _normalize_protocol(protocol: str | None) -> str:
    """BiDi reports 'http/1.1', 'h2', 'h3'."""
    if not protocol:
        return "HTTP/1.1"
    protocol = protocol.lower()
    return {"h2": "HTTP/2", "h3": "HTTP/3"}.get(protocol, protocol.upper())


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


class FirefoxFetcher(Fetcher):
    """Headless Firefox fetcher communicating via W3C WebDriver BiDi (CDP was removed in Firefox 141).

    Response bodies come from a ``network.addDataCollector`` registered on the tab and
    ``network.getData`` per request, so every request the page made (document, assets,
    XHR, lazy content triggered during the settle phase) becomes a transaction with
    its real status line, headers and body. Requests whose body Firefox cannot hand
    over (data: URLs, some cached resources) are recorded with an empty body.
    """

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
    ):
        if websockets is None:
            raise ImportError(
                "The 'websockets' library is required for browser fetchers. "
                "Install it via 'pip install crau[browser]'."
            )
        self.binary_path = str(binary_path) if binary_path else None
        self.host = host
        self.port = port or _find_free_port()
        self.window_size = window_size
        self.timeout = timeout
        self.user_agent = user_agent
        self.extra_args = extra_args or []
        self.settle_timeout = settle_timeout
        self.idle_time = idle_time
        self.min_settle_time = min_settle_time
        self.auto_scroll = auto_scroll
        self._activity = NetworkActivity()
        self._collector_id: str | None = None
        self._request_collector_id: str | None = None

        if user_data_dir:
            self.user_data_dir = Path(user_data_dir).resolve()
            self._is_temp_profile = False
        else:
            self.user_data_dir = Path(tempfile.mkdtemp(prefix="crau-firefox-"))
            self._is_temp_profile = True

        self._proc: asyncio.subprocess.Process | None = None
        self._fetch_lock = asyncio.Lock()
        self._ws: Any = None
        self._msg_id = 0
        self._context_id: str | None = None
        self._session_id: str | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[dict[str, Any]] = []
        self._reader_task: asyncio.Task | None = None

    def _resolve_binary(self) -> str:
        candidates = (
            [self.binary_path]
            if self.binary_path
            else ["firefox-esr", "firefox"]
        )
        for name in candidates:
            if not name:
                continue
            if Path(name).is_file():
                return str(Path(name).resolve())
            found = shutil.which(name)
            if found:
                return found

        raise FileNotFoundError(
            "Firefox binary not found. Install it via 'apt install -y firefox-esr' (Debian/Ubuntu) "
            "or specify --binary-path."
        )

    def _prepare_profile(self) -> None:
        """Create the profile directory; the only preference written is the User-Agent override, when asked.

        WebDriver BiDi needs no preference: ``--remote-debugging-port`` alone opens it (checked on ESR 140 and
        on 156). ``remote.active-protocols`` used to switch CDP on, and CDP was removed in Firefox 141.
        """
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if not self.user_agent:
            return
        user_js = self.user_data_dir / "user.js"
        pref = f'user_pref("general.useragent.override", {json.dumps(self.user_agent)});\n'
        current_content = user_js.read_text(encoding="utf-8") if user_js.exists() else ""
        kept = [line for line in current_content.splitlines() if "general.useragent.override" not in line]
        user_js.write_text("".join(line + "\n" for line in kept) + pref, encoding="utf-8")

    async def _wait_until_ready(self, timeout: float | None = None) -> None:
        # a cold start (binary not in the page cache) can take >10s
        timeout = max(self.timeout, 30.0) if timeout is None else timeout
        start_time = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_time < timeout:
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError(
                    f"Firefox exited prematurely with code {self._proc.returncode}"
                )
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=0.3,
                )
                writer.close()
                await writer.wait_closed()
                return
            except Exception:
                await asyncio.sleep(0.1)

        raise TimeoutError(f"Firefox BiDi did not open port {self.port} within {timeout}s")

    async def _reader_loop(self) -> None:
        async for raw_message in self._ws:
            data = json.loads(raw_message)
            message_id = data.get("id")
            if message_id is not None:
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    if "error" in data or data.get("type") == "error":
                        future.set_exception(RuntimeError(f"BiDi error: {data.get('error')}: {data.get('message')}"))
                    else:
                        future.set_result(data.get("result", {}))
            elif data.get("type") == "event":
                self._events.append(data)
                request_id = data.get("params", {}).get("request", {}).get("request")
                if request_id:
                    if data["method"] == "network.beforeRequestSent":
                        self._activity.started(request_id)
                    elif data["method"] in ("network.responseCompleted", "network.fetchError"):
                        self._activity.finished(request_id)

    async def _send_bidi(self, method: str, params: dict[str, Any] | None = None) -> dict:
        self._msg_id += 1
        cmd_id = self._msg_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future
        await self._ws.send(json.dumps({"id": cmd_id, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except Exception as error:
            raise RuntimeError(f"BiDi error on {method}: {error}") from error

    async def _evaluate(self, expression: str) -> Any:
        """script.evaluate with the full argument set Firefox expects."""
        result = await self._send_bidi(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self._context_id},
                "awaitPromise": True,
                "resultOwnership": "none",
                "serializationOptions": {},
            },
        )
        if result.get("type") == "exception":
            raise RuntimeError(f"Script raised: {result.get('exceptionDetails', {}).get('text')}")
        return result.get("result", {}).get("value")

    async def _init_bidi_session(self) -> None:
        self._ws = await websockets.connect(f"ws://{self.host}:{self.port}/session", max_size=None)
        self._reader_task = asyncio.create_task(self._reader_loop(), name="crau-bidi-reader")

        session = await self._send_bidi("session.new", {"capabilities": {}})
        self._session_id = session.get("sessionId")
        await self._send_bidi(
            "session.subscribe",
            {"events": ["network.beforeRequestSent", "network.responseCompleted", "network.fetchError"]},
        )
        # must precede browsingContext.create so the first document already gets it
        await self._send_bidi("script.addPreloadScript", {"functionDeclaration": WEBDRIVER_PRELOAD_SCRIPT})

        # Firefox opens no tab on its own
        created = await self._send_bidi("browsingContext.create", {"type": "tab"})
        self._context_id = created.get("context")
        collector = await self._send_bidi(
            "network.addDataCollector",
            {"dataTypes": ["response"], "maxEncodedDataSize": 100 * 1024 * 1024, "contexts": [self._context_id]},
        )
        self._collector_id = collector.get("collector")
        try:
            # a second collector: request bodies (POST data) are not part of the network events
            request_collector = await self._send_bidi(
                "network.addDataCollector",
                {"dataTypes": ["request"], "maxEncodedDataSize": 50 * 1024 * 1024, "contexts": [self._context_id]},
            )
            self._request_collector_id = request_collector.get("collector")
        except RuntimeError as error:
            logger.debug("Request bodies will not be captured: %s", error)
        try:
            await self._send_bidi(
                "browsingContext.setViewport",
                {
                    "context": self._context_id,
                    "viewport": {"width": self.window_size[0], "height": self.window_size[1]},
                },
            )
        except RuntimeError as error:
            logger.debug("Could not set viewport: %s", error)

    async def __aenter__(self) -> "FirefoxFetcher":
        resolved_bin = self._resolve_binary()
        self._prepare_profile()

        env = os.environ.copy()
        if not env.get("HOME") or env["HOME"] == "/":
            env["HOME"] = "/tmp"

        cmd = [
            resolved_bin,
            "--headless",
            "--width",
            str(self.window_size[0]),
            "--height",
            str(self.window_size[1]),
            f"--remote-debugging-port={self.port}",
            "-profile",
            str(self.user_data_dir),
        ]
        cmd.extend(self.extra_args)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await self._wait_until_ready()
            await self._init_bidi_session()
            return self
        except Exception:
            await self._stop_process()
            raise

    async def fetch(self, url: str, **kwargs: Any) -> CrawlResult:
        """Navigate to url in the single browsing context; navigations are serialized."""
        async with self._fetch_lock:
            return await self._fetch_unlocked(url, **kwargs)

    async def _fetch_unlocked(self, url: str, evaluate_js: str | None = None, **kwargs: Any) -> CrawlResult:
        if not self._ws or not self._context_id:
            raise RuntimeError("Firefox BiDi session is not ready.")

        start_time = time.perf_counter()
        self._events.clear()
        self._activity.reset()

        navigation = await self._send_bidi(
            "browsingContext.navigate",
            {"context": self._context_id, "url": url, "wait": "complete"},
        )
        navigation_id = navigation.get("navigation")

        settle = {}
        if self.settle_timeout > 0:
            settle = await settle_page(
                self._evaluate,
                self._activity,
                timeout=self.settle_timeout,
                idle=self.idle_time,
                min_time=self.min_settle_time,
                auto_scroll=self.auto_scroll,
            )
            if settle["pending"]:
                logger.debug("%s: %d request(s) still in flight after settle timeout", url, settle["pending"])
        if evaluate_js:
            await self._evaluate(evaluate_js)
        rendered_html = await self._evaluate("document.documentElement.outerHTML") or ""

        transactions, navigations = await self._collect_transactions(list(self._events))
        duration = time.perf_counter() - start_time

        main_tx = None
        for tx, tx_navigation in zip(transactions, navigations):
            if (navigation_id and tx_navigation == navigation_id) or tx.request.url == url:
                main_tx = tx  # keep the last match: redirects produce several
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
            main_transaction=main_tx,
            timing={"total": duration, **settle},
        )
        return CrawlResult(transactions=transactions, page=page)

    async def _get_response_body(self, request_id: str, url: str) -> bytes:
        return await self._get_data(request_id, url, "response", self._collector_id)

    async def _get_request_body(self, request_id: str, url: str) -> bytes:
        return await self._get_data(request_id, url, "request", self._request_collector_id)

    async def _get_data(self, request_id: str, url: str, data_type: str, collector_id: str | None) -> bytes:
        if not collector_id:
            return b""
        try:
            data = await self._send_bidi(
                "network.getData",
                {"request": request_id, "dataType": data_type, "collector": collector_id, "disown": True},
            )
        except RuntimeError as error:
            logger.debug("No %s body for %s: %s", data_type, url, error)
            return b""
        payload = data.get("bytes", {})
        if payload.get("type") == "base64":
            return base64.b64decode(payload.get("value", ""))
        return str(payload.get("value", "")).encode("utf-8")

    async def _collect_transactions(
        self, events: list[dict[str, Any]]
    ) -> tuple[list[NetworkTransaction], list[str | None]]:
        """One transaction per network.responseCompleted, with the navigation id each belongs to.

        A redirect keeps the request id and increments ``redirectCount``, so hops are keyed
        by both; the collected body belongs to the last hop of each request.
        """
        requests: dict[tuple[str, int], dict[str, Any]] = {}
        responses: dict[tuple[str, int], dict[str, Any]] = {}
        order: list[tuple[str, int]] = []
        last_hop: dict[str, int] = {}
        for event in events:
            params = event.get("params", {})
            request_id = params.get("request", {}).get("request")
            if not request_id:
                continue
            key = (request_id, int(params.get("redirectCount") or 0))
            if event["method"] in ("network.beforeRequestSent", "network.responseCompleted"):
                if key not in requests:
                    order.append(key)
                    requests[key] = params
                    last_hop[request_id] = max(last_hop.get(request_id, 0), key[1])
                if event["method"] == "network.responseCompleted":
                    responses[key] = params

        transactions: list[NetworkTransaction] = []
        navigations: list[str | None] = []
        for key in order:
            request_id, hop = key
            response_params = responses.get(key)
            if response_params is None:
                logger.debug("Skipping request without response: %s", requests[key].get("request", {}).get("url"))
                continue
            request_data = response_params.get("request", {})
            response_data = response_params.get("response", {})
            url = request_data.get("url", "")
            body = await self._get_response_body(request_id, url) if hop == last_hop[request_id] else b""
            timings = request_data.get("timings", {})
            timestamp = datetime.datetime.now(datetime.timezone.utc)
            if timings.get("timeOrigin") and timings.get("requestTime") is not None:
                timestamp = datetime.datetime.fromtimestamp(
                    (timings["timeOrigin"] + timings["requestTime"]) / 1000.0, datetime.timezone.utc
                )
            duration = None
            if timings.get("requestTime") is not None and timings.get("responseEnd") is not None:
                duration = (timings["responseEnd"] - timings["requestTime"]) / 1000.0

            request_body = b""
            if request_data.get("bodySize"):
                request_body = await self._get_request_body(request_id, url)
            raw_req = RawRequest(
                url=url,
                method=request_data.get("method", "GET"),
                raw_headers=_bidi_headers_to_tuples(request_data.get("headers")),
                raw_body=request_body,
                timestamp=timestamp,
            )
            raw_resp = RawResponse(
                status_code=int(response_data.get("status", 0)),
                reason_phrase=response_data.get("statusText") or "",
                http_version=_normalize_protocol(response_data.get("protocol")),
                raw_headers=_bidi_headers_to_tuples(response_data.get("headers")),
                raw_body=body,
                timestamp=timestamp,
            )
            transactions.append(NetworkTransaction(request=raw_req, response=raw_resp, duration_seconds=duration))
            navigations.append(response_params.get("navigation"))
        return transactions, navigations

    async def _stop_process(self) -> None:
        if self._ws is not None:
            if self._session_id:
                try:
                    await self._send_bidi("session.end", {})
                except Exception:
                    pass
            if self._reader_task is not None:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._reader_task = None
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
            finally:
                self._proc = None

        if self._is_temp_profile and self.user_data_dir.exists():
            shutil.rmtree(self.user_data_dir, ignore_errors=True)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._stop_process()
