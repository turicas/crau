"""The browser fetchers against real browsers and a local site (skipped when a browser is not installed)."""

import json
import socket
import subprocess
import sys

import pytest
from browsers import require_browser
from testsite import Site

from crau.fetchers.chromium import ChromiumFetcher
from crau.fetchers.firefox import FirefoxFetcher
from crau.fetchers.lightpanda import LightpandaFetcher
from crau.models import create_har_log

FETCHERS = {"chromium": ChromiumFetcher, "firefox": FirefoxFetcher, "lightpanda": LightpandaFetcher}


@pytest.fixture(params=sorted(FETCHERS))
def backend(request) -> str:
    require_browser(request.param)
    return request.param


@pytest.fixture
def site():
    with Site() as running:
        yield running


def paths(result, site) -> list[tuple[str, str, int]]:
    return [(tx.request.method, tx.request.url.replace(site.url, ""), tx.response.status_code) for tx in result.transactions]


@pytest.mark.asyncio
async def test_fetch_records_redirect_hops_bodies_and_late_requests(backend, site):
    async with FETCHERS[backend](settle_timeout=5, min_settle_time=1) as fetcher:
        assert fetcher.port is None  # the browser picked its own port
        redirected = await fetcher.fetch(site.url + "/redirect")
        index = await fetcher.fetch(site.url + "/?q=1")

    assert paths(redirected, site)[:2] == [("GET", "/redirect", 302), ("GET", "/final", 200)]
    assert redirected.page.status_code == 200 and redirected.page.main_transaction is redirected.transactions[1]
    assert b"Final page" in redirected.page.raw_body and "Final page" in redirected.page.content

    assert ("POST", "/api/echo", 200) in paths(index, site)
    post = next(tx for tx in index.transactions if tx.request.method == "POST")
    assert post.request.raw_body == b"field=value&other=1"
    assert json.loads(post.response.body) == {"received": "field=value&other=1"}
    assert index.page.main_transaction.request.url == site.url + "/?q=1"
    assert b'<h1 id="main-title">' in index.page.raw_body
    assert 'id="late"' in index.page.content and b'id="late"' not in index.page.raw_body  # rendered vs wire
    assert index.page.timing["pending"] == 0 and index.page.timing["total"] > 0
    if backend != "lightpanda":
        assert index.page.timing["scroll_steps"] >= 2

    entry = create_har_log(index.transactions)["log"]["entries"][0]
    assert entry["request"]["queryString"] == [{"name": "q", "value": "1"}]


@pytest.mark.asyncio
async def test_user_agent_reaches_the_server(backend, site):
    async with FETCHERS[backend](user_agent="crau-test/1.0", settle_timeout=0) as fetcher:
        result = await fetcher.fetch(site.url + "/echo")
    assert json.loads(result.page.raw_body)["user-agent"] == "crau-test/1.0"


@pytest.mark.asyncio
async def test_explicit_port_and_profile_directory_are_honoured(site, tmp_path):
    require_browser("chromium")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = tmp_path / "profile"
    async with ChromiumFetcher(port=port, user_data_dir=profile, settle_timeout=0) as fetcher:
        assert f":{port}/" in fetcher.browser._process.endpoint
        assert (await fetcher.fetch(site.url + "/final")).page.status_code == 200
    assert (profile / "Default").is_dir() or any(profile.iterdir())  # a persistent profile is left in place


@pytest.mark.parametrize("output_format, filename", [("warc", "site.warc.gz"), ("har", "site.har")])
def test_archive_command_with_a_browser_backend(backend, site, tmp_path, output_format, filename):
    output = tmp_path / filename
    command = [sys.executable, "-m", "crau", "-q", "archive", str(output), site.url + "/redirect", "--backend", backend,
               "--format", output_format, "--max-depth", "0", "--browser-settle", "2"]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    if output_format == "har":
        entries = json.loads(output.read_text())["log"]["entries"]
        statuses = {entry["request"]["url"].replace(site.url, ""): entry["response"]["status"] for entry in entries}
        assert statuses["/redirect"] == 302 and statuses["/final"] == 200
    else:
        listed = subprocess.run([sys.executable, "-m", "crau", "list", str(output)], capture_output=True, text=True, timeout=60)
        assert site.url + "/redirect" in listed.stdout and site.url + "/final" in listed.stdout
