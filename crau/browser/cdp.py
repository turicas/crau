"""Chrome DevTools Protocol backend (Chromium and Lightpanda)."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any

from .connection import CdpConnection
from .errors import JsError, NavigationError, ProtocolError, UnsupportedError, WaitTimeout

_KEYS = {
    "Enter": (13, "\r"),
    "Tab": (9, ""),
    "Escape": (27, ""),
    "Backspace": (8, ""),
    "Delete": (46, ""),
    "ArrowUp": (38, ""),
    "ArrowDown": (40, ""),
    "ArrowLeft": (37, ""),
    "ArrowRight": (39, ""),
    "Home": (36, ""),
    "End": (35, ""),
    "PageUp": (33, ""),
    "PageDown": (34, ""),
    "Space": (32, " "),
}
_LOAD_EVENTS = {"load": "Page.loadEventFired", "domcontentloaded": "Page.domContentEventFired"}


def header_pairs(headers: dict[str, Any] | None) -> list[tuple[str, str]]:
    """CDP header dict to (name, value) pairs; repeated headers arrive joined by newlines."""
    pairs = []
    for name, value in (headers or {}).items():
        pairs.extend((name, line) for line in str(value).split("\n"))
    return pairs


def cookie_to_cdp(cookie: dict[str, Any]) -> dict[str, Any]:
    result = {"name": cookie["name"], "value": cookie["value"], "path": cookie.get("path") or "/"}
    for source, target in (("domain", "domain"), ("url", "url"), ("secure", "secure"), ("http_only", "httpOnly")):
        if cookie.get(source) not in (None, ""):
            result[target] = cookie[source]
    if cookie.get("expires"):
        result["expires"] = float(cookie["expires"])
    if cookie.get("same_site"):
        result["sameSite"] = str(cookie["same_site"]).capitalize()
    return result


def cookie_from_cdp(cookie: dict[str, Any]) -> dict[str, Any]:
    expires = cookie.get("expires")
    return {
        "name": cookie.get("name", ""),
        "value": cookie.get("value", ""),
        "domain": cookie.get("domain", ""),
        "path": cookie.get("path", "/"),
        "secure": bool(cookie.get("secure")),
        "http_only": bool(cookie.get("httpOnly")),
        "expires": expires if isinstance(expires, (int, float)) and expires > 0 else None,
        "same_site": cookie.get("sameSite"),
    }


class CdpBrowser:
    protocol = "cdp"

    def __init__(self, connection: CdpConnection, browser_name: str) -> None:
        self.connection = connection
        self.browser_name = browser_name
        self._tabs: dict[str, CdpTab] = {}
        self._closed: set[str] = set()
        self._headers: dict[str, str] = {}
        self._cookies: list[dict[str, Any]] = []
        connection.add_listener(self._dispatch)

    @classmethod
    async def connect(
        cls, endpoint: str, browser_name: str, timeout: float, stealth: bool = True, insecure: bool = False
    ) -> CdpBrowser:
        return cls(await CdpConnection.connect(endpoint, timeout=timeout), browser_name)

    def _dispatch(self, event: dict[str, Any]) -> None:
        session_id = event.get("sessionId")
        for tab in self._tabs.values():
            if tab.session_id == session_id:
                tab.handle_event(event)
                return

    async def version(self) -> str:
        return (await self.connection.call("Browser.getVersion")).get("product", "")

    async def _attach(self, target_id: str) -> CdpTab:
        attached = await self.connection.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        tab = CdpTab(self, target_id, attached["sessionId"])
        self._tabs[target_id] = tab
        await tab.enable()
        return tab

    async def new_tab(self) -> CdpTab:
        try:
            created = await self.connection.call("Target.createTarget", {"url": "about:blank"})
        except ProtocolError as exc:
            if "TargetAlreadyLoaded" in exc.message:
                raise UnsupportedError("Lightpanda serves a single page per connection: it has no tabs") from exc
            raise
        return await self._attach(created["targetId"])

    async def tabs(self) -> list[CdpTab]:
        """Every open page, attaching to the ones this connection does not drive yet."""
        targets = (await self.connection.call("Target.getTargets")).get("targetInfos", [])
        # a target that was just closed may still be listed for a moment, and would never answer an attach
        pages = [target["targetId"] for target in targets if target.get("type") == "page"]
        pages = [target_id for target_id in pages if target_id not in self._closed]
        for target_id in pages:
            if target_id not in self._tabs:
                await self._attach(target_id)
        for target_id in list(self._tabs):
            if target_id not in pages:
                del self._tabs[target_id]
        return [self._tabs[target_id] for target_id in pages]

    def forget(self, tab: CdpTab) -> None:
        self._tabs.pop(tab.target_id, None)
        self._closed.add(tab.target_id)

    async def set_headers(self, headers: dict[str, str]) -> None:
        self._headers = dict(headers)
        for tab in self._tabs.values():
            await tab.apply_headers(self._headers)

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies.extend(cookies)
        payload = {"cookies": [cookie_to_cdp(cookie) for cookie in cookies]}
        if self.browser_name == "lightpanda":  # no browser-level storage domain: cookies go through the page session
            for tab in self._tabs.values():
                await tab.call("Network.setCookies", payload)
        else:
            await self.connection.call("Storage.setCookies", payload)

    async def cookies(self) -> list[dict[str, Any]]:
        if self.browser_name == "lightpanda":
            tab = next(iter(self._tabs.values()), None)
            raw = (await tab.call("Network.getCookies"))["cookies"] if tab else []
        else:
            raw = (await self.connection.call("Storage.getCookies"))["cookies"]
        return [cookie_from_cdp(cookie) for cookie in raw]

    async def end_session(self) -> None:
        """Nothing to release: CDP allows reconnecting at any time."""

    async def close_browser(self) -> None:
        try:
            await self.connection.call("Browser.close", timeout=5)
        except Exception:  # the socket usually drops before the reply; the process group is killed right after
            pass

    async def disconnect(self) -> None:
        await self.connection.close()


class CdpTab:
    def __init__(self, browser: CdpBrowser, target_id: str, session_id: str) -> None:
        self.browser = browser
        self.target_id = target_id
        self.session_id = session_id
        self.id = target_id
        self.network_listener: Callable[[dict[str, Any]], None] | None = None
        self.console_listener: Callable[[dict[str, Any]], None] | None = None
        self.dialog_listener: Callable[[dict[str, Any]], None] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._hops: dict[str, int] = {}
        self._request_extras: dict[str, int] = {}
        self._response_extras: dict[str, int] = {}

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict:
        return await self.browser.connection.call(method, params, session_id=self.session_id, timeout=timeout)

    raw = call

    async def enable(self) -> None:
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        try:  # bigger buffers: Chromium evicts response bodies beyond its small defaults
            await self.call("Network.enable", {"maxTotalBufferSize": 256 << 20, "maxResourceBufferSize": 64 << 20})
        except ProtocolError:
            await self.call("Network.enable")
        if self.browser._headers:
            await self.apply_headers(self.browser._headers)
        if self.browser.browser_name == "lightpanda" and self.browser._cookies:
            cookies = [cookie_to_cdp(cookie) for cookie in self.browser._cookies]
            await self.call("Network.setCookies", {"cookies": cookies})

    async def apply_headers(self, headers: dict[str, str]) -> None:
        await self.call("Network.setExtraHTTPHeaders", {"headers": headers})

    # --- events ---------------------------------------------------------------------------------------------------

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def handle_event(self, event: dict[str, Any]) -> None:
        method, params = event["method"], event.get("params", {})
        if method.startswith("Network.") and self.network_listener:
            for normalized in self._normalize_network(method, params):
                self.network_listener(normalized)
        elif method == "Runtime.consoleAPICalled" and self.console_listener:
            text = " ".join(str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", []))
            self.console_listener({"level": params.get("type", "log"), "text": text})
        elif method == "Runtime.exceptionThrown" and self.console_listener:
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text", "")
            self.console_listener({"level": "error", "text": text})
        elif method == "Page.javascriptDialogOpening":
            if self.dialog_listener:
                self.dialog_listener({"type": params.get("type"), "message": params.get("message", "")})
            self._spawn(self._accept_dialog())

    async def _accept_dialog(self) -> None:
        try:
            await self.call("Page.handleJavaScriptDialog", {"accept": True})
        except Exception:
            pass

    def _normalize_network(self, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """CDP reuses one requestId along a redirect chain; records are keyed `<requestId>#<hop>` instead.

        The `*ExtraInfo` events carry the headers really sent and received (cookies included) and may arrive before
        or after the main events, so their hop is derived from their own arrival order.
        """
        request_id = params.get("requestId", "")
        if method == "Network.requestWillBeSent":
            events = []
            redirect = params.get("redirectResponse")
            if redirect:
                previous = f"{request_id}#{self._hops.get(request_id, 0)}"
                events.append({"kind": "response", "id": previous, **self._response(redirect)})
                events.append(
                    {"kind": "finished", "id": previous, "encoded_size": redirect.get("encodedDataLength", 0)}
                )
                self._hops[request_id] = self._hops.get(request_id, 0) + 1
            else:
                self._hops.setdefault(request_id, 0)
            request = params.get("request", {})
            events.append(
                {
                    "kind": "request",
                    "id": f"{request_id}#{self._hops[request_id]}",
                    "url": request.get("url", "") + request.get("urlFragment", ""),
                    "method": request.get("method", "GET"),
                    "headers": header_pairs(request.get("headers")),
                    "post_data": request.get("postData"),
                    "has_post_data": bool(request.get("hasPostData")),
                    "time": params.get("wallTime"),
                    "resource_type": params.get("type", ""),
                    "is_navigation": params.get("type") == "Document" and request_id == params.get("loaderId"),
                }
            )
            return events
        if method == "Network.requestWillBeSentExtraInfo":
            hop = self._request_extras.get(request_id, 0)
            self._request_extras[request_id] = hop + 1
            return [
                {"kind": "request_headers", "id": f"{request_id}#{hop}", "headers": header_pairs(params.get("headers"))}
            ]
        if method == "Network.responseReceivedExtraInfo":
            hop = self._response_extras.get(request_id, 0)
            self._response_extras[request_id] = hop + 1
            headers = header_pairs(params.get("headers"))
            return [{"kind": "response_headers", "id": f"{request_id}#{hop}", "headers": headers}]
        key = f"{request_id}#{self._hops.get(request_id, 0)}"
        if method == "Network.responseReceived":
            return [{"kind": "response", "id": key, **self._response(params.get("response", {}))}]
        if method == "Network.loadingFinished":
            return [{"kind": "finished", "id": key, "encoded_size": params.get("encodedDataLength", 0)}]
        if method == "Network.loadingFailed":
            return [{"kind": "failed", "id": key, "error": params.get("errorText", "")}]
        return []

    @staticmethod
    def _response(response: dict[str, Any]) -> dict[str, Any]:
        timing = response.get("timing") or {}
        return {
            "url": response.get("url", ""),
            "status": int(response.get("status", 0)),
            "status_text": response.get("statusText", ""),
            "headers": header_pairs(response.get("headers")),
            "mime_type": response.get("mimeType", ""),
            "protocol": response.get("protocol", ""),
            "remote_ip": response.get("remoteIPAddress", ""),
            "wait_ms": timing.get("receiveHeadersEnd", -1),
        }

    # --- page primitives ------------------------------------------------------------------------------------------

    async def navigate(self, url: str, wait_until: str, timeout: float) -> None:
        connection = self.browser.connection
        waiter = None
        if wait_until in _LOAD_EVENTS:
            expected = _LOAD_EVENTS[wait_until]
            waiter = asyncio.ensure_future(
                connection.wait_for_event(
                    lambda event: event.get("method") == expected and event.get("sessionId") == self.session_id,
                    timeout,
                )
            )
            await asyncio.sleep(0)
        try:
            try:
                result = await self.call("Page.navigate", {"url": url}, timeout=timeout)
            except ProtocolError as exc:
                raise NavigationError(f"Could not load {url}: {exc.message}") from exc
            if result.get("errorText"):
                raise NavigationError(f"Could not load {url}: {result['errorText']}")
            if waiter is not None and result.get("loaderId"):  # same-document navigations fire no load event
                try:
                    await waiter
                except asyncio.TimeoutError:
                    raise WaitTimeout(f"{url} did not reach '{wait_until}' in {timeout:.0f}s") from None
        finally:
            if waiter is not None and not waiter.done():
                waiter.cancel()

    async def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        result = await self.call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": await_promise}
        )
        details = result.get("exceptionDetails")
        if details:
            exception = details.get("exception", {})
            raise JsError(exception.get("description") or exception.get("value") or details.get("text", "JS error"))
        return result.get("result", {}).get("value")

    async def screenshot(self, full_page: bool, clip: dict[str, float] | None, image_format: str, quality: int | None):
        params: dict[str, Any] = {"format": image_format}
        if quality is not None and image_format == "jpeg":
            params["quality"] = quality
        if clip is None and full_page:
            size = (await self.call("Page.getLayoutMetrics")).get("cssContentSize")
            if size:
                clip = {"left": 0, "top": 0, "width": size["width"], "height": size["height"]}
        if clip is not None:
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": clip["left"], "y": clip["top"], "width": clip["width"], "height": clip["height"]}
            params["clip"]["scale"] = 1
        try:
            data = await self.call("Page.captureScreenshot", params, timeout=120)
        except ProtocolError:
            if self.browser.browser_name != "lightpanda":
                raise
            data = await self.call("Page.captureScreenshot", {"format": image_format})
        return base64.b64decode(data["data"])

    async def pdf(self, landscape: bool, background: bool, header: str | None, footer: str | None) -> bytes:
        if self.browser.browser_name == "lightpanda":
            raise UnsupportedError("Lightpanda cannot print to PDF; use chromium or firefox")
        params: dict[str, Any] = {"landscape": landscape, "printBackground": background, "preferCSSPageSize": True}
        if header is not None or footer is not None:
            params.update(
                displayHeaderFooter=True,
                headerTemplate=header or "<span></span>",
                footerTemplate=footer or "<span></span>",
            )
        return base64.b64decode((await self.call("Page.printToPDF", params, timeout=120))["data"])

    async def set_viewport(self, width: int, height: int) -> None:
        await self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 0, "mobile": False},
        )

    async def mouse_click(self, x: float, y: float) -> None:
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        await self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self.call("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        await self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})

    async def insert_text(self, text: str) -> None:
        await self.call("Input.insertText", {"text": text})

    async def press_key(self, key: str) -> None:
        code, text = _KEYS.get(key, (0, key if len(key) == 1 else ""))
        base = {"key": " " if key == "Space" else key, "code": key, "windowsVirtualKeyCode": code}
        await self.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": text, **base})
        await self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    async def response_body(self, key: str) -> bytes | None:
        try:
            body = await self.call("Network.getResponseBody", {"requestId": key.rsplit("#", 1)[0]})
        except ProtocolError:
            return None
        return base64.b64decode(body["body"]) if body.get("base64Encoded") else body.get("body", "").encode("utf-8")

    async def request_body(self, key: str) -> str | None:
        try:
            request_id = key.rsplit("#", 1)[0]
            return (await self.call("Network.getRequestPostData", {"requestId": request_id})).get("postData")
        except ProtocolError:
            return None

    async def start_body_capture(self) -> bool:
        return True

    async def info(self) -> dict[str, str]:
        targets = (await self.browser.connection.call("Target.getTargets")).get("targetInfos", [])
        for target in targets:
            if target.get("targetId") == self.target_id:
                return {"url": target.get("url", ""), "title": target.get("title", "")}
        return {"url": "", "title": ""}

    async def activate(self) -> None:
        await self.browser.connection.call("Target.activateTarget", {"targetId": self.target_id})

    async def close(self) -> None:
        self.browser.forget(self)
        try:
            await self.browser.connection.call("Target.closeTarget", {"targetId": self.target_id})
        except ProtocolError:
            pass
