import asyncio
import base64
import json

import pytest
import pytest_asyncio
import websockets

from crau.fetchers.cdp import CdpBrowserFetcher

RENDERED_HTML = "<html><body>Rendered by CDP</body></html>"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"


class FakeBrowser:
    """Minimal CDP browser: page-level domains only work on an attached session, like Chromium.

    The browser-level connection answers Target.* commands; Page/Network/Runtime commands
    sent without a sessionId get the same error a real browser returns. Events are sent
    with the session's id. Every received command is recorded in `commands`.
    """

    def __init__(self, page_targets: list[dict] | None = None):
        self.page_targets = page_targets if page_targets is not None else [
            {"targetId": "T1", "type": "page", "url": "about:blank", "attached": False}
        ]
        self.commands: list[dict] = []
        self.sessions: dict[str, str] = {}
        self.bodies: dict[str, dict] = {}
        self.scrolls = 0

    async def handler(self, websocket):
        async for message in websocket:
            data = json.loads(message)
            self.commands.append(data)
            method = data["method"]
            session_id = data.get("sessionId")
            reply = {"id": data["id"]}
            if session_id:
                reply["sessionId"] = session_id

            if method == "Target.getTargets":
                reply["result"] = {"targetInfos": list(self.page_targets)}
            elif method == "Target.createTarget":
                target = {"targetId": f"T{len(self.page_targets) + 1}", "type": "page", "url": "about:blank", "attached": False}
                self.page_targets.append(target)
                reply["result"] = {"targetId": target["targetId"]}
            elif method == "Target.attachToTarget":
                new_session = f"S-{data['params']['targetId']}"
                self.sessions[new_session] = data["params"]["targetId"]
                reply["result"] = {"sessionId": new_session}
            elif session_id not in self.sessions:
                reply["error"] = {"code": -32601, "message": f"'{method}' wasn't found"}
            elif method in ("Page.enable", "Network.enable", "Runtime.enable", "Network.setUserAgentOverride"):
                reply["result"] = {}
            elif method == "Page.navigate":
                reply["result"] = {"frameId": "F1", "loaderId": "L1"}
                await websocket.send(json.dumps(reply))
                await self._emit_navigation(websocket, session_id, data["params"]["url"])
                continue
            elif method == "Network.getResponseBody":
                body = self.bodies.get(data["params"]["requestId"])
                if body is None:
                    reply["error"] = {"code": -32000, "message": "No resource with given identifier found"}
                else:
                    reply["result"] = body
            elif method == "Runtime.evaluate":
                expression = data["params"]["expression"]
                if "scrollBy" in expression:
                    # first scroll step reveals a lazy image (IntersectionObserver-style); then the page ends
                    self.scrolls += 1
                    if self.scrolls == 1:
                        await self._emit_request(websocket, session_id, "lazy", "lazy.png", "image/png", PNG_BYTES)
                    reply["result"] = {"result": {"type": "string", "value": json.dumps({"moved": self.scrolls == 1, "height": 900})}}
                else:
                    reply["result"] = {"result": {"type": "string", "value": RENDERED_HTML}}
            else:
                reply["error"] = {"code": -32601, "message": f"'{method}' wasn't found"}
            await websocket.send(json.dumps(reply))

    async def _emit_request(self, websocket, session_id: str, request_id: str, path: str, mime: str, body: bytes) -> None:
        def event(method, params):
            return json.dumps({"method": method, "params": params, "sessionId": session_id})

        url = self.base_url + path
        self.bodies[request_id] = {"body": base64.b64encode(body).decode("ascii"), "base64Encoded": True}
        await websocket.send(event("Network.requestWillBeSent", {"requestId": request_id, "request": {"url": url, "method": "GET", "headers": {}}}))
        await websocket.send(event("Network.responseReceived", {"requestId": request_id, "response": {"url": url, "status": 200, "statusText": "OK", "headers": {"Content-Type": mime}, "protocol": "http/1.1"}}))
        await websocket.send(event("Network.loadingFinished", {"requestId": request_id}))

    async def _emit_late_request(self, websocket, session_id: str) -> None:
        """An XHR fired by a timer after the load event."""
        await asyncio.sleep(0.3)
        await self._emit_request(websocket, session_id, "late", "api/late.json", "application/json", b'{"late": true}')

    async def _emit_navigation(self, websocket, session_id: str, url: str) -> None:
        def event(method, params):
            return json.dumps({"method": method, "params": params, "sessionId": session_id})

        self.base_url = url
        self.scrolls = 0
        self.bodies = {
            "doc": {"body": RENDERED_HTML, "base64Encoded": False},
            "img": {"body": base64.b64encode(PNG_BYTES).decode("ascii"), "base64Encoded": True},
        }
        await websocket.send(
            event(
                "Network.requestWillBeSent",
                {
                    "requestId": "doc",
                    "wallTime": 1700000000.0,
                    "request": {"url": url, "method": "GET", "headers": {"User-Agent": "cdp-agent"}},
                },
            )
        )
        await websocket.send(
            event(
                "Network.responseReceived",
                {
                    "requestId": "doc",
                    "response": {
                        "url": url,
                        "status": 200,
                        "statusText": "OK",
                        "headers": {"Content-Type": "text/html"},
                        "protocol": "h2",
                        "remoteIPAddress": "203.0.113.7",
                        "remotePort": 443,
                        "timing": {"receiveHeadersEnd": 120.0},
                    },
                },
            )
        )
        await websocket.send(
            event(
                "Network.requestWillBeSent",
                {"requestId": "img", "request": {"url": url + "logo.png", "method": "GET", "headers": {}}},
            )
        )
        await websocket.send(
            event(
                "Network.responseReceived",
                {
                    "requestId": "img",
                    "response": {"url": url + "logo.png", "status": 200, "statusText": "OK", "headers": {"Content-Type": "image/png"}, "protocol": "http/1.1"},
                },
            )
        )
        # a request that never gets a response (aborted / blocked)
        await websocket.send(
            event(
                "Network.requestWillBeSent",
                {"requestId": "aborted", "request": {"url": url + "tracker.js", "method": "GET", "headers": {}}},
            )
        )
        await websocket.send(event("Network.loadingFailed", {"requestId": "aborted", "errorText": "net::ERR_BLOCKED_BY_CLIENT"}))
        await websocket.send(event("Network.loadingFinished", {"requestId": "doc"}))
        await websocket.send(event("Network.loadingFinished", {"requestId": "img"}))
        await websocket.send(event("Page.loadEventFired", {"timestamp": 1001.0}))
        asyncio.ensure_future(self._emit_late_request(websocket, session_id))


@pytest_asyncio.fixture
async def run_fake_browser():
    servers = []

    async def _start(browser: FakeBrowser | None = None):
        browser = browser or FakeBrowser()
        server = await websockets.serve(browser.handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        servers.append(server)
        return f"ws://127.0.0.1:{port}", browser

    yield _start

    for server in servers:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cdp_fetcher_attaches_to_page_target_and_captures_traffic(run_fake_browser):
    endpoint, browser = await run_fake_browser()
    async with CdpBrowserFetcher(endpoint_url=endpoint, settle_timeout=5, min_settle_time=0.5, idle_time=0.2) as fetcher:
        assert fetcher._session_id == "S-T1"
        result = await fetcher.fetch("https://example.com/")

    # page-level commands must go through the attached session, never the browser endpoint
    page_commands = [c for c in browser.commands if c["method"].split(".")[0] in ("Page", "Network", "Runtime")]
    assert page_commands and all(c.get("sessionId") == "S-T1" for c in page_commands)

    # requests made after the load event (lazy image on scroll, timer XHR) are captured too
    assert [tx.request.url for tx in result.transactions] == [
        "https://example.com/",
        "https://example.com/logo.png",
        "https://example.com/lazy.png",
        "https://example.com/api/late.json",
    ]
    document, image, lazy, late = result.transactions
    assert lazy.response.body == PNG_BYTES
    assert late.response.body == b'{"late": true}'
    assert result.page.timing["scroll_steps"] >= 2
    assert document.response.status_code == 200
    assert document.response.http_version == "HTTP/2"
    assert document.response.body == RENDERED_HTML.encode()
    assert document.remote_address == ("203.0.113.7", 443)
    assert document.duration_seconds == pytest.approx(0.12)
    assert document.request.timestamp.timestamp() == 1700000000.0
    assert image.response.body == PNG_BYTES
    assert image.duration_seconds is None
    assert result.page.main_transaction is document
    assert result.page.status_code == 200
    assert result.page.content == RENDERED_HTML


@pytest.mark.asyncio
async def test_cdp_fetcher_creates_a_target_when_no_free_page_exists(run_fake_browser):
    endpoint, browser = await run_fake_browser(FakeBrowser(page_targets=[]))
    async with CdpBrowserFetcher(endpoint_url=endpoint) as fetcher:
        assert fetcher._session_id == "S-T1"
    assert any(c["method"] == "Target.createTarget" for c in browser.commands)


@pytest.mark.asyncio
async def test_cdp_fetcher_reports_browser_errors_instead_of_hanging(run_fake_browser):
    endpoint, browser = await run_fake_browser()
    async with CdpBrowserFetcher(endpoint_url=endpoint, timeout=2) as fetcher:
        with pytest.raises(RuntimeError, match="wasn't found"):
            await fetcher.send("Page.navigate", {"url": "https://example.com/"}, session=False)


@pytest.mark.asyncio
async def test_cdp_fetcher_applies_user_agent_override(run_fake_browser):
    endpoint, browser = await run_fake_browser()
    async with CdpBrowserFetcher(endpoint_url=endpoint, user_agent="crau/test"):
        pass
    overrides = [c for c in browser.commands if c["method"] == "Network.setUserAgentOverride"]
    assert overrides and overrides[0]["params"] == {"userAgent": "crau/test"}


@pytest.mark.asyncio
async def test_cdp_fetcher_serializes_concurrent_fetches():
    """One browser tab cannot navigate to two URLs at once: concurrent fetch() calls must not overlap."""
    from unittest.mock import AsyncMock, patch

    fetcher = CdpBrowserFetcher(endpoint_url="ws://127.0.0.1:1")
    active = 0
    max_active = 0

    async def fake_fetch(url, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return url

    with patch.object(fetcher, "_fetch_unlocked", new=AsyncMock(side_effect=fake_fetch)):
        results = await asyncio.gather(*(fetcher.fetch(f"http://x/{i}") for i in range(4)))

    assert sorted(results) == [f"http://x/{i}" for i in range(4)]
    assert max_active == 1


@pytest.mark.asyncio
async def test_cdp_fetcher_settle_disabled_collects_right_after_load(run_fake_browser):
    endpoint, browser = await run_fake_browser()
    async with CdpBrowserFetcher(endpoint_url=endpoint, settle_timeout=0) as fetcher:
        result = await fetcher.fetch("https://example.com/")
    assert [tx.request.url for tx in result.transactions] == ["https://example.com/", "https://example.com/logo.png"]
    assert browser.scrolls == 0


@pytest.mark.asyncio
async def test_cdp_fetcher_settle_without_auto_scroll(run_fake_browser):
    endpoint, browser = await run_fake_browser()
    async with CdpBrowserFetcher(endpoint_url=endpoint, settle_timeout=5, min_settle_time=0.5, auto_scroll=False) as fetcher:
        result = await fetcher.fetch("https://example.com/")
    urls = [tx.request.url for tx in result.transactions]
    assert "https://example.com/api/late.json" in urls
    assert "https://example.com/lazy.png" not in urls
    assert browser.scrolls == 0


class RedirectingBrowser(FakeBrowser):
    """The navigation is answered with a 302: CDP keeps the requestId and reports the hop in `redirectResponse`."""

    async def _emit_navigation(self, websocket, session_id: str, url: str) -> None:
        def event(method, params):
            return json.dumps({"method": method, "params": params, "sessionId": session_id})

        final_url = url + "final"
        self.bodies = {"doc": {"body": RENDERED_HTML, "base64Encoded": False}}
        redirect = {
            "url": url,
            "status": 302,
            "statusText": "Found",
            "headers": {"Location": "/final"},
            "protocol": "http/1.1",
            "remoteIPAddress": "203.0.113.7",
            "remotePort": 80,
        }
        final = {"url": final_url, "status": 200, "statusText": "OK", "headers": {"Content-Type": "text/html"}, "protocol": "http/1.1"}
        for method, params in [
            ("Network.requestWillBeSent", {"requestId": "doc", "request": {"url": url, "method": "GET", "headers": {}}}),
            (
                "Network.requestWillBeSent",
                {"requestId": "doc", "request": {"url": final_url, "method": "GET", "headers": {}}, "redirectResponse": redirect},
            ),
            ("Network.responseReceived", {"requestId": "doc", "response": final}),
            ("Network.loadingFinished", {"requestId": "doc"}),
            ("Page.loadEventFired", {"timestamp": 1001.0}),
        ]:
            await websocket.send(event(method, params))


@pytest.mark.asyncio
async def test_cdp_fetcher_records_every_redirect_hop(run_fake_browser):
    endpoint, browser = await run_fake_browser(RedirectingBrowser())
    async with CdpBrowserFetcher(endpoint_url=endpoint, settle_timeout=0) as fetcher:
        result = await fetcher.fetch("https://example.com/")

    redirect, final = result.transactions
    assert (redirect.request.url, redirect.response.status_code) == ("https://example.com/", 302)
    assert redirect.response.reason_phrase == "Found"
    assert (b"Location", b"/final") in redirect.response.raw_headers
    assert redirect.response.body == b""
    assert redirect.remote_address == ("203.0.113.7", 80)
    assert (final.request.url, final.response.status_code) == ("https://example.com/final", 200)
    assert final.response.body == RENDERED_HTML.encode()
    # the page is the document that was rendered: the last hop of the navigation, not the 302
    assert result.page.main_transaction is final
    assert result.page.status_code == 200
    # the body belongs to the last hop only: asking once is enough
    assert len([c for c in browser.commands if c["method"] == "Network.getResponseBody"]) == 1
