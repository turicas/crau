"""Public API: `Browser` and `Tab`. The same calls work on Chromium, Firefox and Lightpanda.

async with Browser("chromium") as browser:
    tab = await browser.new_tab("https://example.com", wait_selector="h1")
    await tab.fill("label=Search", "open data")
    await tab.click("text=Go")
    await tab.wait(selector=".results")
    links = await tab.evaluate("[...document.querySelectorAll('.results a')].map(a => a.href)")
    await tab.screenshot("results.png", full_page=True)
"""

from __future__ import annotations

import asyncio
import atexit
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crau.version import __version__

from . import instances, javascript
from .bidi import BidiBrowser
from .cdp import CdpBrowser
from .cookies import load_cookies, load_headers
from .errors import BrowserError, ProtocolError, UnsupportedError, WaitTimeout
from .har import build_har
from .launch import DEFAULT_VIEWPORT, PROTOCOLS, BrowserProcess, LaunchOptions
from .network import NetworkMonitor, NetworkRecord

WAIT_UNTIL = ("load", "domcontentloaded", "none")
IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


@dataclass
class Response:
    """Main document of the last navigation."""

    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


def _exit_on_sigterm(signum, frame) -> None:
    raise SystemExit(128 + signum)


class Browser:
    """One browser process (launched here, or a named instance started earlier) and its tabs."""

    def __init__(
        self,
        browser: str = "chromium",
        *,
        headless: bool = True,
        stealth: bool = True,
        profile: str | Path | None = None,
        viewport: tuple[int, int] = DEFAULT_VIEWPORT,
        user_agent: str | None = None,
        cookies: list[dict[str, Any]] | str | Path | None = None,
        headers: dict[str, str] | str | Path | None = None,
        extra_args: list[str] | tuple[str, ...] = (),
        prefs: dict[str, object] | None = None,
        binary: str | Path | None = None,
        sandbox: bool = False,
        insecure: bool = False,
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = browser
        self.timeout = timeout
        self.stealth = stealth
        self.insecure = insecure
        self.viewport = viewport
        self.version = ""
        self.options = LaunchOptions(
            browser=browser,
            binary=Path(binary) if binary else None,
            headless=headless,
            stealth=stealth,
            profile=Path(profile) if profile else None,
            viewport=viewport,
            user_agent=user_agent,
            sandbox=sandbox,
            insecure=insecure,
            host=host,
            port=port,
            extra_args=list(extra_args),
            prefs=dict(prefs or {}),
            timeout=timeout,
        )
        self._initial_cookies = cookies
        self._initial_headers = headers
        self._instance: instances.Instance | None = None
        self._process: BrowserProcess | None = None
        self._backend: CdpBrowser | BidiBrowser | None = None
        self._tabs: dict[str, Tab] = {}
        self._keep_running = False

    @classmethod
    def connect(cls, endpoint: str, browser: str = "chromium", *, stealth: bool = True, timeout: float = 30.0) -> Browser:
        """Drive a browser that is already running, given its WebSocket endpoint. Closing only disconnects.

        `endpoint` is the browser-level CDP URL (`webSocketDebuggerUrl` of `/json/version`) for Chromium and
        Lightpanda, or `ws://host:port/session` for Firefox.
        """
        instance = instances.Instance(
            name="", browser=browser, pid=0, endpoint=endpoint, profile="", run_dir="", temporary_profile=False,
            version="", started_at=0.0, stealth=stealth,
        )  # fmt: skip
        connected = cls(browser, stealth=stealth, timeout=timeout)
        connected._instance = instance
        connected._keep_running = True
        return connected

    @classmethod
    def attach(cls, instance_name: str, *, timeout: float = 30.0) -> Browser:
        """Drive a named instance started with `start_instance()` / `crau browser start`. Closing only detaches."""
        instance = instances.load(instance_name)
        browser = cls(instance.browser, stealth=instance.stealth, insecure=instance.insecure, timeout=timeout)
        browser._instance = instance
        browser._keep_running = True
        return browser

    # --- lifecycle ------------------------------------------------------------------------------------------------

    async def start(self) -> Browser:
        if self._backend is not None:
            return self
        if self._instance is not None:
            endpoint, self.version = self._instance.endpoint, self._instance.version
        else:
            # before launching: a SIGTERM that lands while the browser is starting must still run the cleanup
            if threading.current_thread() is threading.main_thread():
                if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
                    signal.signal(signal.SIGTERM, _exit_on_sigterm)
            process = BrowserProcess(self.options)
            atexit.register(process.stop)
            self._process = await process.start()
            endpoint, self.version = self._process.endpoint, self._process.version
        backend_class = BidiBrowser if PROTOCOLS[self.name] == "bidi" else CdpBrowser
        try:
            self._backend = await backend_class.connect(
                endpoint, self.name, self.timeout, stealth=self.stealth, insecure=self.insecure
            )
            if not self.version:  # connected to an endpoint: ask the browser itself, if it can tell
                try:
                    self.version = await self._backend.version()
                except ProtocolError:
                    self.version = ""
            if self._initial_cookies is not None:
                await self.set_cookies(self._initial_cookies)
            if self._initial_headers is not None:
                await self.set_headers(self._initial_headers)
        except BaseException:
            await self.close()
            raise
        return self

    async def start_instance(self, instance_name: str) -> instances.Instance:
        """Launch, register under `instance_name` and leave running after this process exits."""
        if self.name == "lightpanda":
            raise UnsupportedError("Lightpanda keeps no state between connections, so it cannot be a named instance")
        try:
            instances.load(instance_name)
        except BrowserError:
            pass
        else:
            raise BrowserError(f"Instance {instance_name!r} is already running")
        await self.start()
        self._instance = instances.new(instance_name, self.name, self._process, self.stealth, self.insecure)
        self._keep_running = True
        atexit.unregister(self._process.stop)
        return self._instance

    async def close(self) -> None:
        """Quit the browser (or just detach from a named instance). Safe to call more than once."""
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                if self._keep_running:
                    await backend.end_session()
                else:
                    await backend.close_browser()
                await backend.disconnect()
            except Exception:
                pass
        if self._process is not None:
            atexit.unregister(self._process.stop)
            if self._keep_running:
                self._process.detach()
            else:
                # Lightpanda ignores the protocol-level close and has no profile to flush: do not wait for it
                await self._process.wait_and_stop(grace=0.0 if self.name == "lightpanda" else 5.0)
        self._process = None
        self._tabs.clear()

    async def __aenter__(self) -> Browser:
        return await self.start()

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    def _require_backend(self):
        if self._backend is None:
            raise BrowserError("Browser is not started: use 'async with Browser(...)' or 'await browser.start()'")
        return self._backend

    # --- tabs -----------------------------------------------------------------------------------------------------

    def _wrap(self, backend_tab) -> Tab:
        if backend_tab.id not in self._tabs:
            self._tabs[backend_tab.id] = Tab(self, backend_tab)
        return self._tabs[backend_tab.id]

    async def new_tab(self, url: str | None = None, *, apply_viewport: bool = True, **goto_options: Any) -> Tab:
        """Open a tab; with `url`, also navigate (accepts every `Tab.goto` option)."""
        tab = self._wrap(await self._require_backend().new_tab())
        if apply_viewport and self.name != "lightpanda":
            await tab.set_viewport(*self.viewport)
        if url is not None:
            await tab.goto(url, **goto_options)
        return tab

    async def tabs(self) -> list[Tab]:
        """Open tabs, in browser order. Tabs are addressed by position: ids change between Firefox sessions."""
        backend_tabs = await self._require_backend().tabs()
        alive = {backend_tab.id for backend_tab in backend_tabs}
        self._tabs = {tab_id: tab for tab_id, tab in self._tabs.items() if tab_id in alive}
        return [self._wrap(backend_tab) for backend_tab in backend_tabs]

    # --- cookies and headers --------------------------------------------------------------------------------------

    async def set_cookies(self, cookies: list[dict[str, Any]] | str | Path) -> None:
        """Add cookies: a list of dicts or a cookie file (Netscape cookies.txt or JSON)."""
        if isinstance(cookies, (str, Path)):
            cookies = load_cookies(Path(cookies))
        await self._require_backend().set_cookies(list(cookies))

    async def cookies(self) -> list[dict[str, Any]]:
        return await self._require_backend().cookies()

    async def set_headers(self, headers: dict[str, str] | str | Path) -> None:
        """Extra HTTP headers sent with every request: a dict or a headers file (JSON object or `Name: value`)."""
        if isinstance(headers, (str, Path)):
            headers = load_headers(Path(headers))
        await self._require_backend().set_headers(dict(headers))


class Tab:
    """A page. Element targets are CSS selectors, `text=Visible text` or `label=Field label`."""

    def __init__(self, browser: Browser, backend_tab) -> None:
        self.browser = browser
        self._backend = backend_tab
        self.console: list[dict[str, Any]] = []
        self.dialogs: list[dict[str, Any]] = []
        self.response: Response | None = None
        self._monitor = NetworkMonitor(backend_tab)
        backend_tab.network_listener = self._monitor.handle
        backend_tab.console_listener = self.console.append
        backend_tab.dialog_listener = self.dialogs.append

    @property
    def timeout(self) -> float:
        return self.browser.timeout

    # --- navigation and waiting -----------------------------------------------------------------------------------

    async def goto(
        self,
        url: str,
        *,
        wait_until: str = "load",
        wait_selector: str | None = None,
        wait_js: str | None = None,
        wait_ms: int = 0,
        wait_idle: bool = False,
        scroll: bool = False,
        timeout: float | None = None,
    ) -> Response | None:
        """Navigate and wait. Returns the main document's `Response` (None when the browser reported none)."""
        if wait_until not in WAIT_UNTIL:
            raise ValueError(f"wait_until must be one of {', '.join(WAIT_UNTIL)}")
        timeout = self.timeout if timeout is None else timeout
        started = time.perf_counter()
        self._monitor.reset_navigation()
        self.response = None
        await self._backend.navigate(url, wait_until, timeout)
        remaining = max(timeout - (time.perf_counter() - started), 1.0)
        await self.wait(
            selector=wait_selector, js=wait_js, ms=wait_ms, idle=wait_idle, scroll=scroll, timeout=remaining
        )
        record = self._monitor.main_document(url)
        if record is not None:
            headers = {name.lower(): value for name, value in record.response_headers}
            self.response = Response(url=record.url, status=record.status, headers=headers)
        return self.response

    async def wait(
        self,
        *,
        selector: str | None = None,
        js: str | None = None,
        ms: int = 0,
        idle: bool = False,
        scroll: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Wait for: an element to exist, a JS expression to be truthy, the network to go quiet (optionally scrolling
        to the bottom to trigger lazy content), then a fixed delay. Element and JS waits raise `WaitTimeout`; the idle
        wait is a best effort capped by `timeout`, because pages with polling or streaming never go quiet."""
        timeout = self.timeout if timeout is None else timeout
        if selector:
            await self._poll(javascript.call(javascript.EXISTS, selector), timeout, f"element {selector!r}")
        if js:
            await self._poll(f"Boolean({js})", timeout, f"expression {js!r}")
        if idle or scroll:
            await self.settle(min(timeout, 15.0), scroll=scroll)
        if ms:
            await asyncio.sleep(ms / 1000)

    async def _poll(self, expression: str, timeout: float, description: str) -> None:
        deadline = time.perf_counter() + timeout
        while True:
            try:
                if await self._backend.evaluate(expression, await_promise=False):
                    return
            except ProtocolError:
                pass  # execution context destroyed by a navigation in progress: try again
            if time.perf_counter() >= deadline:
                raise WaitTimeout(f"Timed out after {timeout:.1f}s waiting for {description}")
            await asyncio.sleep(0.1)

    async def settle(
        self, timeout: float = 15.0, *, idle: float = 0.5, minimum: float = 1.0, scroll: bool = False
    ) -> dict[str, Any]:
        """Wait for late requests (XHR, lazy images, infinite scroll) and report what happened.

        With `scroll`, moves one viewport at a time until the page stops growing. Returns once at least `minimum`
        seconds have passed and nothing has started or finished for `idle` seconds with no request in flight, or
        when `timeout` is reached (the hard cap for pages that never go quiet). The result has `settle_seconds`,
        `scroll_steps` and `pending` (requests still in flight).
        """
        if self.browser.name == "lightpanda":
            # no layout: scrollHeight is a constant 100000000 and scrollY grows forever, so the bottom never comes
            scroll = False
        started = time.perf_counter()
        at_bottom = not scroll
        last_height = None
        scroll_steps = 0
        while time.perf_counter() - started < timeout:
            if not at_bottom:
                step = await self._backend.evaluate(javascript.call(javascript.SCROLL_STEP), await_promise=False) or {}
                scroll_steps += 1
                at_bottom = not step.get("moved") and step.get("height") == last_height
                last_height = step.get("height")
            elapsed = time.perf_counter() - started
            if at_bottom and elapsed >= minimum and not self._monitor.inflight and self._monitor.quiet_for() >= idle:
                break
            await asyncio.sleep(0.1)
        return {
            "settle_seconds": time.perf_counter() - started,
            "scroll_steps": scroll_steps,
            "pending": len(self._monitor.inflight),
        }

    # --- reading --------------------------------------------------------------------------------------------------

    async def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        """Run JavaScript and return its value as plain Python (promises are awaited). Raises `JsError`."""
        return await self._backend.evaluate(expression, await_promise=await_promise)

    async def call_function(self, function_source: str, *arguments: Any) -> Any:
        """Call a JS function source (`"(a, b) => a + b"`) with JSON-serializable arguments."""
        return await self._backend.evaluate(javascript.call(function_source, *arguments))

    async def html(self) -> str:
        return await self.call_function(javascript.HTML)

    async def text(self) -> str:
        return await self.call_function(javascript.TEXT)

    async def title(self) -> str:
        return await self._backend.evaluate("document.title", await_promise=False) or ""

    async def url(self) -> str:
        return await self._backend.evaluate("location.href", await_promise=False) or ""

    async def info(self) -> dict[str, str]:
        """`url` and `title` without running script when possible (works on privileged pages too)."""
        return await self._backend.info()

    async def outline(self) -> dict[str, Any]:
        """Headings, links, buttons and form fields with role, name and a CSS selector: a cheap map of the page."""
        return await self.call_function(javascript.OUTLINE)

    async def exists(self, target: str) -> bool:
        return bool(await self.call_function(javascript.EXISTS, target))

    # --- acting ---------------------------------------------------------------------------------------------------

    async def click(self, target: str, *, timeout: float | None = None) -> None:
        """Wait for the element, scroll it into view and click its centre with real mouse events."""
        await self.wait(selector=target, timeout=timeout)
        box = await self.call_function(javascript.ELEMENT_BOX, target)
        # Lightpanda has no layout: it reports made-up boxes and accepts mouse events that hit nothing
        if self.browser.name != "lightpanda" and box and box["hit"] and box["width"] > 0 and box["height"] > 0:
            try:
                await self._backend.mouse_click(box["x"], box["y"])
                return
            except ProtocolError:
                pass  # engines without input emulation
        await self.call_function(javascript.CLICK, target)

    async def fill(self, target: str, value: str | bool, *, timeout: float | None = None) -> None:
        """Set a field's value (input, textarea, select, checkbox, radio, contenteditable) and fire input/change."""
        await self.wait(selector=target, timeout=timeout)
        if not await self.call_function(javascript.FILL, target, value):
            raise BrowserError(f"Could not fill {target!r} with {value!r} (no such option?)")

    async def type(self, target: str, text: str, *, timeout: float | None = None) -> None:
        """Focus the element and send real key events (for widgets that listen to keystrokes, e.g. autocomplete)."""
        await self.wait(selector=target, timeout=timeout)
        await self.call_function(javascript.FOCUS, target)
        await self._backend.insert_text(text)

    async def press(self, key: str) -> None:
        """Press a key on the focused element: Enter, Tab, Escape, Backspace, ArrowDown... or a single character."""
        await self._backend.press_key(key)

    async def set_viewport(self, width: int, height: int) -> None:
        await self._backend.set_viewport(width, height)

    # --- capturing ------------------------------------------------------------------------------------------------

    async def screenshot(
        self,
        path: str | Path | None = None,
        *,
        full_page: bool = False,
        selector: str | None = None,
        image_format: str | None = None,
        quality: int | None = None,
    ) -> bytes:
        """PNG (default) or JPEG of the viewport, the whole page or one element. Format follows the file extension."""
        if image_format is None:
            image_format = IMAGE_FORMATS.get(Path(path).suffix.lower(), "png") if path else "png"
        clip = None
        if selector:
            await self.wait(selector=selector)
            clip = await self.call_function(javascript.ELEMENT_BOX, selector)
        data = await self._backend.screenshot(full_page, clip, image_format, quality)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        return data

    async def pdf(
        self,
        path: str | Path | None = None,
        *,
        landscape: bool = False,
        background: bool = True,
        header: str | None = None,
        footer: str | None = None,
    ) -> bytes:
        """Print to PDF. `header`/`footer` are Chromium HTML templates (classes pageNumber, totalPages, date, title)."""
        data = await self._backend.pdf(landscape, background, header, footer)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        return data

    async def start_recording(self) -> bool:
        """Record every request from now on, bodies included. False when this browser cannot provide bodies."""
        await self._monitor.start_recording()
        return self._monitor.bodies_available

    async def stop_recording(self) -> list[NetworkRecord]:
        return await self._monitor.stop_recording()

    async def har(self) -> dict[str, Any]:
        """Stop recording and return everything captured as a HAR 1.2 dict (unfiltered)."""
        return build_har(await self.stop_recording(), self.browser.version, __version__)

    # --- protocol escape hatches and tab management ---------------------------------------------------------------

    async def cdp(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Raw CDP command on this tab's session (Chromium, Lightpanda)."""
        if self.browser._backend.protocol != "cdp":
            raise UnsupportedError(f"{self.browser.name} speaks BiDi; use tab.bidi()")
        return await self._backend.raw(method, params)

    async def bidi(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Raw BiDi command (Firefox). `context` is not added automatically: this tab's id is `tab.id`."""
        if self.browser._backend.protocol != "bidi":
            raise UnsupportedError(f"{self.browser.name} speaks CDP; use tab.cdp()")
        return await self._backend.raw(method, params)

    @property
    def id(self) -> str:
        return self._backend.id

    async def activate(self) -> None:
        await self._backend.activate()

    async def close(self) -> None:
        self.browser._tabs.pop(self._backend.id, None)
        await self._backend.close()
