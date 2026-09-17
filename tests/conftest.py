import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


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
