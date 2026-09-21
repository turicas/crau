"""Browsers are started on port 0 and tell which port they got: picking a "free" port beforehand is a race."""

import asyncio
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import pytest

from crau.fetchers.chromium import ChromiumFetcher
from crau.fetchers.firefox import FirefoxFetcher
from crau.fetchers.lightpanda import LightpandaFetcher

LISTENING_LINES = {
    "chromium": "DevTools listening on ws://127.0.0.1:{port}/devtools/browser/abc",
    "firefox": "WebDriver BiDi listening on ws://127.0.0.1:{port}",
    "lightpanda": '$scope=note $level=note $msg="server running" address=127.0.0.1:{port} max_connections=16',
}


@pytest.fixture
def debug_endpoint():
    """Stands in for the browser's debugging port: accepts connections and answers /json/version.

    Runs in a thread because ChromiumFetcher polls it with a blocking urllib call.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            body = json.dumps(
                {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc", "User-Agent": "Chrome/151.0.0.0"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def fake_browser(tmp_path, name: str, port: int):
    """Executable that records its arguments, announces `port` the way the real browser does and stays alive."""
    script = tmp_path / f"fake-{name}"
    line = LISTENING_LINES[name].format(port=port)
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Chromium 151.0.7922.137"; exit 0; fi\n'
        f'echo "$@" > {tmp_path / "arguments"}\n'
        f"echo '{line}' >&2\n"
        "exec sleep 30\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def recorded_arguments(tmp_path) -> list[str]:
    """Arguments the fake browser was started with (it may still be writing them when the port already answers)."""
    for _ in range(100):
        if (tmp_path / "arguments").exists() and (tmp_path / "arguments").read_text().strip():
            break
        await asyncio.sleep(0.02)
    return (tmp_path / "arguments").read_text().split()


def test_no_port_is_reserved_before_the_browser_starts():
    for fetcher_class in (ChromiumFetcher, FirefoxFetcher, LightpandaFetcher):
        assert fetcher_class(binary_path="/bin/true").port is None
        assert fetcher_class(binary_path="/bin/true", port=9222).port == 9222


@pytest.mark.asyncio
async def test_chromium_reads_the_port_it_was_given(tmp_path, debug_endpoint):
    script = fake_browser(tmp_path, "chromium", debug_endpoint)
    with patch("crau.fetchers.cdp.CdpBrowserFetcher.__aenter__", new_callable=AsyncMock):
        async with ChromiumFetcher(binary_path=str(script), timeout=5) as fetcher:
            assert fetcher.port == debug_endpoint
            assert fetcher.endpoint_url == f"ws://127.0.0.1:{debug_endpoint}/devtools/browser/abc"
    assert "--remote-debugging-port=0" in await recorded_arguments(tmp_path)


@pytest.mark.asyncio
async def test_firefox_reads_the_port_it_was_given(tmp_path, debug_endpoint):
    script = fake_browser(tmp_path, "firefox", debug_endpoint)
    with patch.object(FirefoxFetcher, "_init_bidi_session", new_callable=AsyncMock):
        async with FirefoxFetcher(binary_path=str(script), timeout=5) as fetcher:
            assert fetcher.port == debug_endpoint
    assert "--remote-debugging-port=0" in await recorded_arguments(tmp_path)


@pytest.mark.asyncio
async def test_lightpanda_reads_the_port_it_was_given(tmp_path, debug_endpoint):
    script = fake_browser(tmp_path, "lightpanda", debug_endpoint)
    with patch("crau.fetchers.cdp.CdpBrowserFetcher.__aenter__", new_callable=AsyncMock):
        async with LightpandaFetcher(binary_path=str(script), timeout=5) as fetcher:
            assert fetcher.port == debug_endpoint
            assert fetcher.endpoint_url == f"ws://127.0.0.1:{debug_endpoint}"
    assert (await recorded_arguments(tmp_path))[:5] == ["serve", "--host", "127.0.0.1", "--port", "0"]


@pytest.mark.asyncio
async def test_explicit_port_is_still_honoured(tmp_path, debug_endpoint):
    script = fake_browser(tmp_path, "firefox", debug_endpoint)
    with patch.object(FirefoxFetcher, "_init_bidi_session", new_callable=AsyncMock):
        async with FirefoxFetcher(binary_path=str(script), port=debug_endpoint, timeout=5) as fetcher:
            assert fetcher.port == debug_endpoint
            arguments = await recorded_arguments(tmp_path)
    assert f"--remote-debugging-port={debug_endpoint}" in arguments
