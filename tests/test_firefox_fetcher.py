import json

import websockets
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from crau.fetchers.firefox import FirefoxFetcher


def test_firefox_fetcher_missing_binary():
    with patch("shutil.which", return_value=None):
        fetcher = FirefoxFetcher(binary_path="nonexistent-firefox")
        with pytest.raises(FileNotFoundError, match="Firefox binary not found"):
            fetcher._resolve_binary()


@pytest.mark.asyncio
async def test_firefox_fetcher_prepares_user_js_and_profile(tmp_path):
    custom_profile = tmp_path / "custom-firefox-profile"

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()

    async def fake_wait():
        return 0

    fake_proc.wait = fake_wait

    with patch("shutil.which", return_value="/usr/bin/firefox-esr"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            with patch.object(FirefoxFetcher, "_wait_until_ready", new_callable=AsyncMock):
                with patch.object(FirefoxFetcher, "_init_bidi_session", new_callable=AsyncMock):
                    fetcher = FirefoxFetcher(user_data_dir=custom_profile, port=9223)
                    async with fetcher:
                        assert fetcher.user_data_dir == custom_profile
                        assert not fetcher._is_temp_profile

                    # user.js must exist and contain the 3 remote preferences
                    user_js = custom_profile / "user.js"
                    assert user_js.exists()
                    content = user_js.read_text()
                    assert 'user_pref("remote.active-protocols", 3);' in content
                    assert 'user_pref("devtools.debugger.remote-enabled", true);' in content
                    assert 'user_pref("devtools.debugger.prompt-connection", false);' in content

                    cmd_args = mock_exec.call_args[0]
                    assert "-profile" in cmd_args
                    assert str(custom_profile) in cmd_args
                    fake_proc.terminate.assert_called_once()


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
                assert data["params"]["contexts"] == ["ctx-1"]
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
                    ("network.beforeRequestSent", {"navigation": "nav-1", "request": doc_request}),
                    ("network.responseCompleted", {"navigation": "nav-1", "request": doc_request, "response": {"url": url, "status": 404, "statusText": "Not Found", "protocol": "http/1.1", "headers": headers}}),
                    ("network.beforeRequestSent", {"navigation": None, "request": img_request}),
                    ("network.responseCompleted", {"navigation": None, "request": img_request, "response": {"url": img_url, "status": 200, "statusText": "OK", "protocol": "h2", "headers": [{"name": "Content-Type", "value": {"type": "string", "value": "image/png"}}]}}),
                ]:
                    await websocket.send(json.dumps({"type": "event", "method": event_method, "params": params}))
            elif method == "script.evaluate":
                assert "serializationOptions" in data["params"]
                if "scrollBy" in data["params"]["expression"]:
                    reply["result"] = {"type": "success", "result": {"type": "string", "value": json.dumps({"moved": False, "height": 500})}}
                else:
                    reply["result"] = {"type": "success", "result": {"type": "string", "value": "<html>rendered</html>"}}
            await websocket.send(json.dumps(reply))


@pytest.mark.asyncio
async def test_firefox_fetcher_bidi_session_and_real_status():
    browser = FakeBidiBrowser()
    server = await websockets.serve(browser.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        fetcher = FirefoxFetcher(binary_path="/bin/true", port=port, settle_timeout=3, min_settle_time=0.3, idle_time=0.1)
        await fetcher._init_bidi_session()
        try:
            result = await fetcher.fetch("http://example.com/missing")
        finally:
            await fetcher._stop_process()
    finally:
        server.close()
        await server.wait_closed()

    methods = [c["method"] for c in browser.commands]
    assert methods.index("session.subscribe") < methods.index("browsingContext.create")
    # the webdriver preload must exist before the first document is created
    assert methods.index("script.addPreloadScript") < methods.index("browsingContext.create")
    preload = [c for c in browser.commands if c["method"] == "script.addPreloadScript"][0]
    assert "webdriver" in preload["params"]["functionDeclaration"]

    assert methods.index("network.addDataCollector") > methods.index("browsingContext.create")

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


def test_firefox_user_agent_goes_into_profile_prefs(tmp_path):
    fetcher = FirefoxFetcher(binary_path="/bin/true", user_data_dir=tmp_path, user_agent="crau/ff")
    fetcher._prepare_profile()
    assert 'user_pref("general.useragent.override", "crau/ff");' in (tmp_path / "user.js").read_text()
