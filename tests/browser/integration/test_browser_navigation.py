from __future__ import annotations

import json
import socket

import pytest
from crau.browser import Browser, JsError, NavigationError, WaitTimeout


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_goto_reports_main_document_and_page_content(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab()
        response = await tab.goto(site.url + "/")
        assert (response.status, response.ok) == (200, True)
        assert response.headers["content-type"].startswith("text/html")
        assert await tab.title() == "Test site"
        assert await tab.url() == site.url + "/"
        html = await tab.html()
        assert html.lower().startswith("<!doctype html>") and '<h1 id="main-title">Test site</h1>' in html
        assert "The bottom" in await tab.text()
        assert browser.version


async def test_new_tab_with_url_and_wait_selector_sees_late_content(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/", wait_selector="#late")
        assert await tab.evaluate("document.querySelector('#late').textContent") == "hydrated"


async def test_wait_for_missing_element_and_false_expression_time_out(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/final")
        with pytest.raises(WaitTimeout, match="#never"):
            await tab.wait(selector="#never", timeout=0.5)
        with pytest.raises(WaitTimeout):
            await tab.wait(js="window.neverDefined === 1", timeout=0.5)
        await tab.wait(js="document.title === 'Final'", timeout=2)


async def test_evaluate_returns_plain_python_values(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/final")
        value = await tab.evaluate(
            "({text: 'á', n: 1.5, flag: true, list: [1, 'x', null], nested: {deep: [{}]}, gone: undefined})"
        )
        assert value == {"text": "á", "n": 1.5, "flag": True, "list": [1, "x", None], "nested": {"deep": [{}]}}
        assert await tab.evaluate("undefined") is None
        assert await tab.evaluate("new Promise((resolve) => setTimeout(() => resolve(7), 50))") == 7
        assert await tab.evaluate("const a = 2; a * 21") == 42
        assert await tab.call_function("(a, b) => a + b.length", 1, ["x", "y"]) == 3


async def test_javascript_exception_is_raised_with_its_message(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/final")
        with pytest.raises(JsError, match="notDefinedAnywhere"):
            await tab.evaluate("notDefinedAnywhere.property")


async def test_unreachable_server_raises_navigation_error(browser_name):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab()
        with pytest.raises(NavigationError):
            await tab.goto(f"http://127.0.0.1:{free_port()}/", timeout=15)


async def test_http_error_status_is_reported_not_raised(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab()
        response = await tab.goto(site.url + "/nothing-here")
        assert (response.status, response.ok) == (404, False)
        assert "Not found" in await tab.text()


async def test_redirect_reports_the_final_document(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab()
        response = await tab.goto(site.url + "/redirect")
        assert response.status == 200 and response.url.endswith("/final")
        assert await tab.url() == site.url + "/final"


async def test_network_idle_wait_covers_requests_fired_after_load(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/slow", wait_idle=True)
        assert await tab.evaluate("document.querySelector('#out').textContent") == "slow-done"


async def test_scroll_wait_reaches_the_bottom_of_the_page(full_browser_name, site):
    async with Browser(full_browser_name) as browser:
        tab = await browser.new_tab(site.url + "/", scroll=True)
        assert await tab.evaluate(
            "window.scrollY > 0 && window.innerHeight + window.scrollY >= document.body.scrollHeight - 2"
        )


async def test_json_document_is_delivered_raw(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/json")
        assert json.loads(await tab.text()) == {"items": [{"id": 1, "name": "one"}], "total": 1}


async def test_console_messages_are_collected(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/", wait_selector="#late")
        assert any("index loaded" in entry["text"] and "42" in entry["text"] for entry in tab.console)


async def test_alert_does_not_block_the_page(full_browser_name, site):
    async with Browser(full_browser_name) as browser:
        tab = await browser.new_tab(site.url + "/dialog")
        await tab.wait(js="document.title === 'after-alert'", timeout=10)
        assert tab.dialogs and tab.dialogs[0]["message"] == "hello from alert"


async def test_viewport_is_applied(full_browser_name, site):
    async with Browser(full_browser_name, viewport=(1000, 700)) as browser:
        tab = await browser.new_tab(site.url + "/final")
        assert await tab.evaluate("[window.innerWidth, window.innerHeight]") == [1000, 700]
        await tab.set_viewport(640, 480)
        assert await tab.evaluate("[window.innerWidth, window.innerHeight]") == [640, 480]
