from __future__ import annotations

import http.server
import shutil
import ssl
import subprocess
import threading

import pytest
from crau.browser import Browser, NavigationError


@pytest.fixture
def https_url(tmp_path):
    """A server with a self-signed certificate: what a TLS-intercepting proxy looks like to the browser."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to create the self-signed certificate")
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(certificate)],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>secure</title><p>over tls</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


async def test_invalid_certificate_is_refused_by_default_and_accepted_with_insecure(browser_name, https_url):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab()
        with pytest.raises(NavigationError):
            await tab.goto(https_url, timeout=15)
    async with Browser(browser_name, insecure=True) as browser:
        tab = await browser.new_tab(https_url)
        assert await tab.title() == "secure"
