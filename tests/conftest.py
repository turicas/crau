import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent / "browser"))  # fakes, testsite, browsers

from fakes import FakeWebSocketServer  # noqa: E402


class _Site:
    """Tiny HTTP site running in a background thread, usable from synchronous tests (e.g. the CLI).

    `pages` maps a path to (content_type, body bytes). Every request is appended to `requests`.
    """

    def __init__(self, pages: dict[str, tuple[str, bytes]]):
        self.pages = pages
        self.requests: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                site.requests.append(self.path)
                page = site.pages.get(self.path)
                if page is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                content_type, body = page
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def threaded_site():
    sites = []

    def _start(pages: dict[str, tuple[str, bytes]]) -> _Site:
        site = _Site(pages)
        sites.append(site)
        return site

    yield _start

    for site in sites:
        site.close()


class _ScriptedConnection:
    """What the scripted fake browsers expect from a server connection: iterate messages, `send` replies."""

    def __init__(self, connection):
        self.connection = connection

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self.connection.receive()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, text: str) -> None:
        await self.connection.send(text)


@pytest_asyncio.fixture
async def serve_fake_browser():
    """Start a fake browser (an object with `async handler(websocket)`) and return its ws:// URL."""
    servers = []

    async def _start(browser) -> str:
        async def handle(connection):
            await browser.handler(_ScriptedConnection(connection))

        server = FakeWebSocketServer(handle)
        await server.__aenter__()
        servers.append(server)
        return server.url

    yield _start

    for server in servers:
        await server.__aexit__(None, None, None)
