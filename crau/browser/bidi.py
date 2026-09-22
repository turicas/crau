"""WebDriver BiDi backend (Firefox)."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .connection import BidiConnection, deserialize_remote_value
from .errors import BrowserError, JsError, NavigationError, ProtocolError, UnsupportedError, WaitTimeout

# Firefox reports navigator.webdriver === true under remote control. Defining the getter on the prototype (where the
# real one lives) leaves no own property on `navigator` to give the override away.
STEALTH_PRELOAD = (
    "() => { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false, configurable: true }); }"
)
_EVENTS = [
    "network.beforeRequestSent",
    "network.responseCompleted",
    "network.fetchError",
    "log.entryAdded",
    "browsingContext.contextCreated",
    "browsingContext.userPromptOpened",
]
_KEYS = {
    "Enter": "\ue007",
    "Tab": "\ue004",
    "Escape": "\ue00c",
    "Backspace": "\ue003",
    "Delete": "\ue017",
    "ArrowUp": "\ue013",
    "ArrowDown": "\ue015",
    "ArrowLeft": "\ue012",
    "ArrowRight": "\ue014",
    "Home": "\ue011",
    "End": "\ue010",
    "PageUp": "\ue00e",
    "PageDown": "\ue00f",
    "Space": " ",
}
_WAIT = {"load": "complete", "domcontentloaded": "interactive", "none": "none"}
# Firefox rejects collectors bigger than its total budget (200 MB by default): try from the top down
_COLLECTOR_SIZES = (200_000_000, 50_000_000, 10_000_000)


def header_pairs(headers: list[dict[str, Any]] | None) -> list[tuple[str, str]]:
    pairs = []
    for header in headers or []:
        value = header.get("value", {})
        pairs.append((header.get("name", ""), str(value.get("value", "")) if isinstance(value, dict) else str(value)))
    return pairs


def cookie_to_bidi(cookie: dict[str, Any]) -> dict[str, Any]:
    domain = cookie.get("domain") or urlsplit(cookie.get("url", "")).hostname or ""
    result: dict[str, Any] = {
        "name": cookie["name"],
        "value": {"type": "string", "value": str(cookie["value"])},
        "domain": domain.lstrip("."),
        "path": cookie.get("path") or "/",
        "httpOnly": bool(cookie.get("http_only")),
        "secure": bool(cookie.get("secure")),
    }
    if cookie.get("expires"):
        result["expiry"] = int(cookie["expires"])
    if cookie.get("same_site"):
        result["sameSite"] = str(cookie["same_site"]).lower()
    return result


def cookie_from_bidi(cookie: dict[str, Any]) -> dict[str, Any]:
    value = cookie.get("value", {})
    same_site = cookie.get("sameSite")
    return {
        "name": cookie.get("name", ""),
        "value": value.get("value", "") if isinstance(value, dict) else value,
        "domain": cookie.get("domain", ""),
        "path": cookie.get("path", "/"),
        "secure": bool(cookie.get("secure")),
        "http_only": bool(cookie.get("httpOnly")),
        "expires": cookie.get("expiry"),
        "same_site": same_site.capitalize() if isinstance(same_site, str) else None,
    }


class BidiBrowser:
    protocol = "bidi"

    def __init__(self, connection: BidiConnection, browser_name: str) -> None:
        self.connection = connection
        self.browser_name = browser_name
        self.version_text = ""
        self._tabs: dict[str, BidiTab] = {}
        self._parents: dict[str, str] = {}
        self._collectors: dict[str, str] = {}
        self._session_open = False
        connection.add_listener(self._dispatch)

    @classmethod
    async def connect(
        cls, endpoint: str, browser_name: str, timeout: float, stealth: bool = True, insecure: bool = False
    ) -> BidiBrowser:
        browser = cls(await BidiConnection.connect(endpoint, timeout=timeout), browser_name)
        try:
            await browser._open_session(stealth, insecure)
        except BaseException:
            await browser.disconnect()
            raise
        return browser

    async def _open_session(self, stealth: bool, insecure: bool) -> None:
        wanted = {"unhandledPromptBehavior": {"default": "accept"}, "acceptInsecureCerts": insecure}
        try:
            session = await self.connection.call("session.new", {"capabilities": {"alwaysMatch": wanted}})
        except ProtocolError as exc:
            if "Maximum number of active sessions" in exc.message:
                raise BrowserError(
                    "Firefox already has an active BiDi session (a previous client did not end it cleanly). "
                    "Stop this browser and start a new one."
                ) from exc
            fallback = {"alwaysMatch": {"acceptInsecureCerts": insecure}}
            session = await self.connection.call("session.new", {"capabilities": fallback})
        self._session_open = True
        capabilities = session.get("capabilities", {})
        self.version_text = f"{capabilities.get('browserName', 'firefox')} {capabilities.get('browserVersion', '')}"
        await self.connection.call("session.subscribe", {"events": _EVENTS})
        if stealth:
            await self.connection.call("script.addPreloadScript", {"functionDeclaration": STEALTH_PRELOAD})

    def _top_level(self, context: str | None) -> str | None:
        seen = set()
        while context in self._parents and context not in seen:
            seen.add(context)
            context = self._parents[context]
        return context

    def _dispatch(self, event: dict[str, Any]) -> None:
        params = event.get("params", {})
        if event["method"] == "browsingContext.contextCreated":
            if params.get("parent"):
                self._parents[params["context"]] = params["parent"]
            return
        context = params.get("context") or params.get("source", {}).get("context")
        tab = self._tabs.get(self._top_level(context))
        if tab is not None:
            tab.handle_event(event, is_top_level=context == tab.context)

    async def version(self) -> str:
        return self.version_text

    async def new_tab(self) -> BidiTab:
        created = await self.connection.call("browsingContext.create", {"type": "tab"})
        tab = BidiTab(self, created["context"])
        self._tabs[tab.context] = tab
        return tab

    async def tabs(self) -> list[BidiTab]:
        tree = await self.connection.call("browsingContext.getTree", {"maxDepth": 0})
        contexts = [entry["context"] for entry in tree.get("contexts", [])]
        for context in contexts:
            if context not in self._tabs:
                self._tabs[context] = BidiTab(self, context)
        for context in list(self._tabs):
            if context not in contexts:
                del self._tabs[context]
        return [self._tabs[context] for context in contexts]

    def forget(self, tab: BidiTab) -> None:
        self._tabs.pop(tab.context, None)

    async def set_headers(self, headers: dict[str, str]) -> None:
        payload = [{"name": name, "value": {"type": "string", "value": value}} for name, value in headers.items()]
        try:
            await self.connection.call("network.setExtraHeaders", {"headers": payload})
        except ProtocolError as exc:
            if exc.code == "unknown command":
                raise UnsupportedError(
                    f"{self.version_text} has no network.setExtraHeaders (Firefox ESR 140 predates it); "
                    "install a current Firefox release to send extra headers"
                ) from exc
            raise

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        for cookie in cookies:
            await self.connection.call("storage.setCookie", {"cookie": cookie_to_bidi(cookie)})

    async def cookies(self) -> list[dict[str, Any]]:
        result = await self.connection.call("storage.getCookies", {})
        return [cookie_from_bidi(cookie) for cookie in result.get("cookies", [])]

    async def start_body_capture(self) -> bool:
        """Register data collectors so bodies can be fetched later. False when this Firefox predates them."""
        for data_type in ("response", "request"):
            if data_type in self._collectors:
                continue
            for size in _COLLECTOR_SIZES:
                try:
                    result = await self.connection.call(
                        "network.addDataCollector", {"dataTypes": [data_type], "maxEncodedDataSize": size}
                    )
                except ProtocolError as exc:
                    if exc.code == "invalid argument":
                        continue
                    break  # unknown command: this Firefox predates data collectors
                self._collectors[data_type] = result["collector"]
                break
        return "response" in self._collectors

    async def get_data(self, data_type: str, request_id: str) -> bytes | None:
        collector = self._collectors.get(data_type)
        if collector is None:
            return None
        try:
            result = await self.connection.call(
                "network.getData", {"dataType": data_type, "collector": collector, "request": request_id}
            )
        except ProtocolError:
            return None
        payload = result.get("bytes", {})
        if payload.get("type") == "base64":
            return base64.b64decode(payload.get("value", ""))
        return str(payload.get("value", "")).encode("utf-8")

    async def end_session(self) -> None:
        """Firefox allows one session at a time and keeps it after a disconnect: always end it explicitly."""
        if self._session_open:
            self._session_open = False
            try:
                await self.connection.call("session.end", timeout=5)
            except Exception:
                pass

    async def close_browser(self) -> None:
        self._session_open = False
        try:
            await self.connection.call("browser.close", timeout=5)
        except Exception:
            pass

    async def disconnect(self) -> None:
        await self.end_session()
        await self.connection.close()


class BidiTab:
    def __init__(self, browser: BidiBrowser, context: str) -> None:
        self.browser = browser
        self.context = context
        self.id = context
        self.network_listener: Callable[[dict[str, Any]], None] | None = None
        self.console_listener: Callable[[dict[str, Any]], None] | None = None
        self.dialog_listener: Callable[[dict[str, Any]], None] | None = None

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict:
        return await self.browser.connection.call(method, {"context": self.context, **(params or {})}, timeout=timeout)

    async def raw(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict:
        return await self.browser.connection.call(method, params, timeout=timeout)

    # --- events ---------------------------------------------------------------------------------------------------

    def handle_event(self, event: dict[str, Any], is_top_level: bool) -> None:
        method, params = event["method"], event.get("params", {})
        if method.startswith("network.") and self.network_listener:
            for normalized in self._normalize_network(method, params, is_top_level):
                self.network_listener(normalized)
        elif method == "log.entryAdded" and self.console_listener:
            self.console_listener({"level": params.get("level", "info"), "text": params.get("text") or ""})
        elif method == "browsingContext.userPromptOpened" and self.dialog_listener:
            self.dialog_listener({"type": params.get("type"), "message": params.get("message", "")})

    def _normalize_network(self, method: str, params: dict[str, Any], is_top_level: bool) -> list[dict[str, Any]]:
        request = params.get("request", {})
        key = f"{request.get('request', '')}#{params.get('redirectCount', 0)}"
        timings = request.get("timings", {})
        if method == "network.beforeRequestSent":
            return [
                {
                    "kind": "request",
                    "id": key,
                    "url": request.get("url", ""),
                    "method": request.get("method", "GET"),
                    "headers": header_pairs(request.get("headers")),
                    "post_data": None,
                    "has_post_data": bool(request.get("bodySize")),
                    "time": params.get("timestamp", 0) / 1000.0,
                    "resource_type": request.get("destination", "") or request.get("initiatorType", "") or "",
                    "is_navigation": bool(params.get("navigation")) and is_top_level,
                }
            ]
        if method == "network.responseCompleted":
            response = params.get("response", {})
            wait = -1
            if timings.get("responseStart") and timings.get("requestTime"):
                wait = timings["responseStart"] - timings["requestTime"]
            return [
                {"kind": "request_headers", "id": key, "headers": header_pairs(request.get("headers"))},
                {
                    "kind": "response",
                    "id": key,
                    "url": response.get("url", ""),
                    "status": int(response.get("status", 0)),
                    "status_text": response.get("statusText", ""),
                    "headers": header_pairs(response.get("headers")),
                    "mime_type": response.get("mimeType", ""),
                    "protocol": response.get("protocol", ""),
                    "remote_ip": "",
                    "wait_ms": wait,
                },
                {"kind": "finished", "id": key, "encoded_size": response.get("bytesReceived", 0)},
            ]
        if method == "network.fetchError":
            return [{"kind": "failed", "id": key, "error": params.get("errorText", "")}]
        return []

    # --- page primitives ------------------------------------------------------------------------------------------

    async def navigate(self, url: str, wait_until: str, timeout: float) -> None:
        try:
            await self.call("browsingContext.navigate", {"url": url, "wait": _WAIT[wait_until]}, timeout=timeout)
        except ProtocolError as exc:
            raise NavigationError(f"Could not load {url}: {exc.message or exc.code}") from exc
        except BrowserError as exc:
            if "no reply" in str(exc):
                raise WaitTimeout(f"{url} did not reach '{wait_until}' in {timeout:.0f}s") from None
            raise

    async def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        result = await self.browser.connection.call(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self.context},
                "awaitPromise": await_promise,
                "resultOwnership": "none",
            },
        )
        if result.get("type") == "exception":
            details = result.get("exceptionDetails", {})
            raise JsError(details.get("text") or "JS error")
        return deserialize_remote_value(result.get("result", {}))

    async def screenshot(self, full_page: bool, clip: dict[str, float] | None, image_format: str, quality: int | None):
        params: dict[str, Any] = {"origin": "document" if full_page or clip else "viewport"}
        params["format"] = {"type": f"image/{image_format}"}
        if quality is not None and image_format == "jpeg":
            params["format"]["quality"] = quality / 100
        if clip is not None:
            params["clip"] = {"type": "box", "x": clip["left"], "y": clip["top"]}
            params["clip"].update(width=clip["width"], height=clip["height"])
        return base64.b64decode((await self.call("browsingContext.captureScreenshot", params, timeout=120))["data"])

    async def pdf(self, landscape: bool, background: bool, header: str | None, footer: str | None) -> bytes:
        if header is not None or footer is not None:
            raise UnsupportedError("BiDi printing has no header/footer templates; use chromium for those")
        params = {"background": background, "orientation": "landscape" if landscape else "portrait"}
        return base64.b64decode((await self.call("browsingContext.print", params, timeout=120))["data"])

    async def set_viewport(self, width: int, height: int) -> None:
        await self.call("browsingContext.setViewport", {"viewport": {"width": width, "height": height}})

    async def _perform(self, source: dict[str, Any]) -> None:
        await self.call("input.performActions", {"actions": [source]})
        await self.call("input.releaseActions")

    async def mouse_click(self, x: float, y: float) -> None:
        actions = [
            {"type": "pointerMove", "x": round(x), "y": round(y), "origin": "viewport"},
            {"type": "pointerDown", "button": 0},
            {"type": "pointerUp", "button": 0},
        ]
        await self._perform(
            {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": actions}
        )

    async def insert_text(self, text: str) -> None:
        actions = []
        for character in text:
            actions += [{"type": "keyDown", "value": character}, {"type": "keyUp", "value": character}]
        await self._perform({"type": "key", "id": "keyboard", "actions": actions})

    async def press_key(self, key: str) -> None:
        value = _KEYS.get(key, key)
        actions = [{"type": "keyDown", "value": value}, {"type": "keyUp", "value": value}]
        await self._perform({"type": "key", "id": "keyboard", "actions": actions})

    async def start_body_capture(self) -> bool:
        return await self.browser.start_body_capture()

    async def response_body(self, key: str) -> bytes | None:
        return await self.browser.get_data("response", key.rsplit("#", 1)[0])

    async def request_body(self, key: str) -> str | None:
        data = await self.browser.get_data("request", key.rsplit("#", 1)[0])
        return None if data is None else data.decode("utf-8", "replace")

    async def info(self) -> dict[str, str]:
        tree = await self.browser.connection.call("browsingContext.getTree", {"root": self.context, "maxDepth": 0})
        url = tree["contexts"][0].get("url", "") if tree.get("contexts") else ""
        try:
            title = await asyncio.wait_for(self.evaluate("document.title", await_promise=False), 5)
        except Exception:  # privileged pages (about:home) refuse script evaluation
            title = ""
        return {"url": url, "title": title or ""}

    async def activate(self) -> None:
        await self.call("browsingContext.activate")

    async def close(self) -> None:
        self.browser.forget(self)
        try:
            await self.call("browsingContext.close")
        except ProtocolError:
            pass
