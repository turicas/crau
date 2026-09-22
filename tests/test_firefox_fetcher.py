import json

import pytest

from crau.browser.errors import LaunchError
from crau.fetchers.firefox import FirefoxFetcher


@pytest.mark.asyncio
async def test_firefox_fetcher_missing_binary():
    with pytest.raises(LaunchError, match="nonexistent-firefox"):
        async with FirefoxFetcher(binary_path="nonexistent-firefox"):
            pass


def test_firefox_fetcher_passes_its_options_to_the_browser(tmp_path):
    fetcher = FirefoxFetcher(
        binary_path="/opt/firefox/firefox", user_data_dir=tmp_path / "profile", port=9223, window_size=(800, 600),
        user_agent="crau/ff", extra_args=["--safe-mode"],
    )  # fmt: skip
    options = fetcher._create_browser().options
    assert (options.browser, str(options.binary), options.profile) == ("firefox", "/opt/firefox/firefox", tmp_path / "profile")
    assert (options.port, options.viewport, options.user_agent, options.extra_args) == (
        9223, (800, 600), "crau/ff", ["--safe-mode"],
    )  # fmt: skip
    assert FirefoxFetcher().port is None  # no port is reserved beforehand: the browser picks one


class FakeBidiBrowser:
    """Minimal WebDriver BiDi endpoint: records commands, emits network events on navigate."""

    def __init__(self):
        self.commands = []

    async def handler(self, websocket):
        async for message in websocket:
            data = json.loads(message)
            self.commands.append(data)
            method = data["method"]
            reply = {"type": "success", "id": data["id"], "result": {}}
            if method == "session.new":
                reply["result"] = {"sessionId": "sess-1", "capabilities": {}}
            elif method == "script.addPreloadScript":
                reply["result"] = {"script": "preload-1"}
            elif method == "browsingContext.create":
                reply["result"] = {"context": "ctx-1"}
            elif method == "network.addDataCollector":
                reply["result"] = {"collector": "col-1"}
            elif method == "network.getData":
                assert data["params"]["collector"] == "col-1" and data["params"]["disown"] is True
                bodies = {"req-doc": ("string", "<html>wire</html>"), "req-img": ("base64", "iVBORw0KGgo=")}
                kind, value = bodies[data["params"]["request"]]
                reply["result"] = {"bytes": {"type": kind, "value": value}}
            elif method == "browsingContext.navigate":
                url = data["params"]["url"]
                reply["result"] = {"navigation": "nav-1", "url": url}
                headers = [{"name": "Content-Type", "value": {"type": "string", "value": "text/html; charset=utf-8"}},
                           {"name": "Content-Length", "value": {"type": "string", "value": "999"}}]
                doc_request = {"request": "req-doc", "url": url, "method": "GET", "headers": [{"name": "Host", "value": {"type": "string", "value": "example.com"}}], "timings": {"timeOrigin": 1700000000000.0, "requestTime": 5.0, "responseEnd": 45.0}}
                img_url = url + "/pic.png"
                img_request = {"request": "req-img", "url": img_url, "method": "GET", "headers": [], "timings": {}}
                for event_method, params in [
                    ("network.beforeRequestSent", {"context": "ctx-1", "navigation": "nav-1", "request": doc_request}),
                    ("network.responseCompleted", {"context": "ctx-1", "navigation": "nav-1", "request": doc_request, "response": {"url": url, "status": 404, "statusText": "Not Found", "protocol": "http/1.1", "headers": headers}}),
                    ("network.beforeRequestSent", {"context": "ctx-1", "navigation": None, "request": img_request}),
                    ("network.responseCompleted", {"context": "ctx-1", "navigation": None, "request": img_request, "response": {"url": img_url, "status": 200, "statusText": "OK", "protocol": "h2", "headers": [{"name": "Content-Type", "value": {"type": "string", "value": "image/png"}}]}}),
                ]:
                    await websocket.send(json.dumps({"type": "event", "method": event_method, "params": params}))
            elif method == "script.evaluate":
                assert data["params"]["target"] == {"context": "ctx-1"}
                if "scrollBy" in data["params"]["expression"]:
                    step = [["moved", {"type": "boolean", "value": False}], ["height", {"type": "number", "value": 500}]]
                    reply["result"] = {"type": "success", "result": {"type": "object", "value": step}}
                else:
                    reply["result"] = {"type": "success", "result": {"type": "string", "value": "<html>rendered</html>"}}
            await websocket.send(json.dumps(reply))


@pytest.mark.asyncio
async def test_firefox_fetcher_bidi_session_and_real_status(serve_fake_browser):
    browser = FakeBidiBrowser()
    endpoint = await serve_fake_browser(browser) + "/session"
    async with FirefoxFetcher(endpoint_url=endpoint, settle_timeout=3, min_settle_time=0.3, idle_time=0.1) as fetcher:
        result = await fetcher.fetch("http://example.com/missing")

    methods = [c["method"] for c in browser.commands]
    assert methods.index("session.subscribe") < methods.index("browsingContext.create")
    # the webdriver preload must exist before the first document is created
    assert methods.index("script.addPreloadScript") < methods.index("browsingContext.create")
    preload = [c for c in browser.commands if c["method"] == "script.addPreloadScript"][0]
    assert "webdriver" in preload["params"]["functionDeclaration"]
    # on the prototype, where the native getter lives: an own property on `navigator` would give the override away
    assert "Navigator.prototype" in preload["params"]["functionDeclaration"]

    assert methods.index("network.addDataCollector") > methods.index("browsingContext.create")
    assert methods[-1] == "session.end"  # Firefox keeps a session after a disconnect: it must be ended explicitly

    document, image = result.transactions
    assert document.response.status_code == 404
    assert document.response.reason_phrase == "Not Found"
    assert document.request.raw_headers == [(b"Host", b"example.com")]
    # headers and body are the real ones (network.getData), not the rendered DOM
    assert (b"Content-Length", b"999") in document.response.raw_headers
    assert document.response.body == b"<html>wire</html>"
    assert document.duration_seconds == pytest.approx(0.04)
    assert document.request.timestamp.timestamp() == pytest.approx(1700000000.005)
    assert image.response.body == b"\x89PNG\r\n\x1a\n"
    assert image.response.http_version == "HTTP/2"
    assert image.duration_seconds is None
    assert result.page.main_transaction is document
    assert result.page.status_code == 404
    assert result.page.content == "<html>rendered</html>"


def test_firefox_user_agent_goes_into_profile_prefs_once(tmp_path):
    from crau.browser.launch import BrowserProcess

    (tmp_path / "user.js").write_text('user_pref("browser.startup.page", 0);\n')
    options = FirefoxFetcher(user_data_dir=tmp_path, user_agent="crau/ff")._create_browser().options
    for _ in range(2):  # reusing a profile must not pile up duplicates
        process = BrowserProcess(options)
        process.profile = tmp_path
        process._prepare_profile()
    content = (tmp_path / "user.js").read_text()
    assert content.count('user_pref("general.useragent.override", "crau/ff");') == 1
    assert 'user_pref("browser.startup.page", 0);' in content
    assert "remote.active-protocols" not in content  # BiDi needs no preference


class RedirectingBidiBrowser(FakeBidiBrowser):
    """The navigation is answered with a 302: BiDi keeps the request id and increments `redirectCount`."""

    async def handler(self, websocket):
        async for message in websocket:
            data = json.loads(message)
            self.commands.append(data)
            method = data["method"]
            reply = {"type": "success", "id": data["id"], "result": {}}
            if method == "session.new":
                reply["result"] = {"sessionId": "sess-1", "capabilities": {}}
            elif method == "browsingContext.create":
                reply["result"] = {"context": "ctx-1"}
            elif method == "network.addDataCollector":
                reply["result"] = {"collector": "col-1"}
            elif method == "network.getData":
                reply["result"] = {"bytes": {"type": "string", "value": "<html>final</html>"}}
            elif method == "browsingContext.navigate":
                url = data["params"]["url"]
                reply["result"] = {"navigation": "nav-1", "url": url + "final"}
                location = [{"name": "Location", "value": {"type": "string", "value": "/final"}}]
                first = {"request": "req-doc", "url": url, "method": "GET", "headers": [], "timings": {}}
                second = {"request": "req-doc", "url": url + "final", "method": "GET", "headers": [], "timings": {}}
                found = {"url": url, "status": 302, "statusText": "Found", "protocol": "http/1.1", "headers": location}
                ok = {"url": url + "final", "status": 200, "statusText": "OK", "protocol": "http/1.1", "headers": []}
                for event_method, params in [
                    ("network.beforeRequestSent", {"context": "ctx-1", "navigation": "nav-1", "redirectCount": 0, "request": first}),
                    ("network.responseCompleted", {"context": "ctx-1", "navigation": "nav-1", "redirectCount": 0, "request": first, "response": found}),
                    ("network.beforeRequestSent", {"context": "ctx-1", "navigation": "nav-1", "redirectCount": 1, "request": second}),
                    ("network.responseCompleted", {"context": "ctx-1", "navigation": "nav-1", "redirectCount": 1, "request": second, "response": ok}),
                ]:
                    await websocket.send(json.dumps({"type": "event", "method": event_method, "params": params}))
            elif method == "script.evaluate":
                reply["result"] = {"type": "success", "result": {"type": "string", "value": "<html>rendered</html>"}}
            await websocket.send(json.dumps(reply))


@pytest.mark.asyncio
async def test_firefox_fetcher_records_every_redirect_hop(serve_fake_browser):
    browser = RedirectingBidiBrowser()
    endpoint = await serve_fake_browser(browser) + "/session"
    async with FirefoxFetcher(endpoint_url=endpoint, settle_timeout=0) as fetcher:
        result = await fetcher.fetch("http://example.com/")

    redirect, final = result.transactions
    assert (redirect.request.url, redirect.response.status_code) == ("http://example.com/", 302)
    assert (b"Location", b"/final") in redirect.response.raw_headers
    assert redirect.response.body == b""
    assert (final.request.url, final.response.status_code) == ("http://example.com/final", 200)
    assert final.response.body == b"<html>final</html>"
    assert result.page.main_transaction is final
    assert result.page.status_code == 200
    # the collected data belongs to the last hop only: asking once is enough
    assert len([c for c in browser.commands if c["method"] == "network.getData"]) == 1


class PostingBidiBrowser(FakeBidiBrowser):
    """The page sends a POST: the body only exists in a `request` data collector."""

    def __init__(self, request_collector: bool = True):
        super().__init__()
        self.request_collector = request_collector

    async def handler(self, websocket):
        async for message in websocket:
            data = json.loads(message)
            self.commands.append(data)
            method = data["method"]
            reply = {"type": "success", "id": data["id"], "result": {}}
            if method == "session.new":
                reply["result"] = {"sessionId": "sess-1", "capabilities": {}}
            elif method == "browsingContext.create":
                reply["result"] = {"context": "ctx-1"}
            elif method == "network.addDataCollector":
                kind = data["params"]["dataTypes"][0]
                if kind == "request" and not self.request_collector:
                    reply = {"type": "error", "id": data["id"], "error": "invalid argument", "message": "dataTypes"}
                else:
                    reply["result"] = {"collector": f"col-{kind}"}
            elif method == "network.getData":
                assert data["params"]["collector"] == f"col-{data['params']['dataType']}"
                value = "field=value" if data["params"]["dataType"] == "request" else '{"ok": true}'
                reply["result"] = {"bytes": {"type": "string", "value": value}}
            elif method == "browsingContext.navigate":
                url = data["params"]["url"]
                reply["result"] = {"navigation": "nav-1", "url": url}
                request = {"request": "req-post", "url": url, "method": "POST", "headers": [], "bodySize": 11, "timings": {}}
                response = {"url": url, "status": 200, "statusText": "OK", "protocol": "http/1.1", "headers": []}
                for event_method, params in [
                    ("network.beforeRequestSent", {"context": "ctx-1", "navigation": "nav-1", "request": request}),
                    ("network.responseCompleted", {"context": "ctx-1", "navigation": "nav-1", "request": request, "response": response}),
                ]:
                    await websocket.send(json.dumps({"type": "event", "method": event_method, "params": params}))
            elif method == "script.evaluate":
                reply["result"] = {"type": "success", "result": {"type": "string", "value": "<html></html>"}}
            await websocket.send(json.dumps(reply))


@pytest.mark.asyncio
@pytest.mark.parametrize("request_collector, expected_body", [(True, b"field=value"), (False, b"")])
async def test_firefox_fetcher_captures_request_bodies_when_the_browser_can(
    serve_fake_browser, request_collector, expected_body
):
    browser = PostingBidiBrowser(request_collector=request_collector)
    endpoint = await serve_fake_browser(browser) + "/session"
    async with FirefoxFetcher(endpoint_url=endpoint, settle_timeout=0) as fetcher:
        result = await fetcher.fetch("http://example.com/submit")

    (transaction,) = result.transactions
    assert transaction.request.method == "POST"
    assert transaction.request.raw_body == expected_body
    assert transaction.response.body == b'{"ok": true}'
