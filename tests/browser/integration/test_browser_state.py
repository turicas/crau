from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from crau.browser import Browser, BrowserError, UnsupportedError, instances
from crau.browser.launch import group_is_running


async def echoed_headers(tab, site) -> dict:
    await tab.goto(site.url + "/final")
    return json.loads(await tab.evaluate("fetch('/echo').then((reply) => reply.text())"))


async def test_cookies_from_list_and_from_netscape_file_reach_the_server(browser_name, site, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tfrom_file\tyes\n")
    async with Browser(browser_name, cookies=cookie_file) as browser:
        tab = await browser.new_tab()
        await browser.set_cookies([{"name": "from_list", "value": "1", "domain": "127.0.0.1"}])
        sent = (await echoed_headers(tab, site))["cookie"]
        assert "from_file=yes" in sent and "from_list=1" in sent
        stored = {cookie["name"]: cookie for cookie in await browser.cookies()}
        assert stored["from_list"]["value"] == "1" and stored["from_file"]["domain"].endswith("127.0.0.1")


async def test_extra_headers_from_dict_and_file_reach_the_server(browser_name, site, tmp_path):
    header_file = tmp_path / "headers.txt"
    header_file.write_text("# comment\nX-From-File: file value\n")
    async with Browser(browser_name, headers=header_file) as browser:
        tab = await browser.new_tab()
        assert (await echoed_headers(tab, site))["x-from-file"] == "file value"
        await browser.set_headers({"X-Token": "abc", "Accept-Language": "pt-BR"})
        headers = await echoed_headers(tab, site)
        assert headers["x-token"] == "abc" and headers["accept-language"] == "pt-BR"


async def test_tabs_are_independent_and_listed_in_order(full_browser_name, site):
    async with Browser(full_browser_name) as browser:
        first = await browser.new_tab(site.url + "/final")
        second = await browser.new_tab(site.url + "/form")
        assert (await first.title(), await second.title()) == ("Final", "Form")
        urls = [(await tab.info())["url"] for tab in await browser.tabs()]
        assert urls[-2:] == [site.url + "/final", site.url + "/form"]
        assert (await browser.tabs())[-1] is second
        await second.close()
        assert [(await tab.info())["url"] for tab in await browser.tabs()][-1] == site.url + "/final"


async def test_lightpanda_has_a_single_tab(site):
    from browsers import require_browser

    require_browser("lightpanda")
    async with Browser("lightpanda") as browser:
        await browser.new_tab(site.url + "/final")
        with pytest.raises(UnsupportedError, match="single page"):
            await browser.new_tab()


async def test_har_keeps_everything_bodies_posts_redirects_cookies(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab()
        assert await tab.start_recording() is True
        await tab.goto(site.url + "/set-cookie")
        await tab.goto(site.url + "/?q=1&empty=", wait_idle=True)
        await tab.goto(site.url + "/redirect")
        await tab.evaluate("fetch('/binary').then((reply) => reply.arrayBuffer()).then(() => true)")
        har = await tab.har()
    assert har["log"]["version"] == "1.2" and har["log"]["creator"]["name"] == "crau"
    entries = har["log"]["entries"]
    by_path = {}
    for entry in entries:
        by_path.setdefault(entry["request"]["url"].replace(site.url, ""), []).append(entry)
    index = by_path["/?q=1&empty="][0]
    assert index["response"]["status"] == 200 and '<h1 id="main-title">' in index["response"]["content"]["text"]
    assert index["request"]["queryString"] == [{"name": "q", "value": "1"}, {"name": "empty", "value": ""}]
    assert index["startedDateTime"].startswith("20") and index["response"]["content"]["mimeType"].startswith(
        "text/html"
    )
    post = by_path["/api/echo"][0]
    assert post["request"]["method"] == "POST" and post["request"]["postData"]["text"] == "field=value&other=1"
    assert json.loads(post["response"]["content"]["text"]) == {"received": "field=value&other=1"}
    redirect = by_path["/redirect"][0]
    assert redirect["response"]["status"] == 302 and redirect["response"]["redirectURL"] == "/final"
    assert by_path["/final"][0]["response"]["status"] == 200
    binary = by_path["/binary"][0]["response"]["content"]
    assert binary["encoding"] == "base64" and base64.b64decode(binary["text"]) == bytes(range(256))
    set_cookie = by_path["/set-cookie"][0]["response"]
    assert {"name": "session", "value": "abc123"} in set_cookie["cookies"]
    sent_cookie = [header for header in index["request"]["headers"] if header["name"].lower() == "cookie"]
    # Lightpanda (nightly 9608) reports the Cookie header one character short in its CDP events, although the server
    # receives it whole; the HAR stores what the browser reported
    expected = "session=abc12" if browser_name == "lightpanda" else "session=abc123"
    assert sent_cookie and expected in sent_cookie[0]["value"]
    assert index["request"]["cookies"][0]["name"] == "session"


async def test_profile_directory_keeps_logins_between_runs(full_browser_name, site, tmp_path):
    profile = tmp_path / "profile"
    async with Browser(full_browser_name, profile=profile) as browser:
        await browser.new_tab(site.url + "/set-cookie?persist=1")
    assert any(profile.iterdir())
    async with Browser(full_browser_name, profile=profile) as browser:
        tab = await browser.new_tab()
        assert "session=abc123" in (await echoed_headers(tab, site)).get("cookie", "")


async def test_named_instance_survives_and_is_reattached_repeatedly(full_browser_name, site, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    name = f"test-{full_browser_name}"
    starter = Browser(full_browser_name)
    instance = await starter.start_instance(name)
    try:
        tab = await starter.new_tab(site.url + "/set-cookie")
        await starter.close()
        assert instance.alive and [item.name for item in instances.list_all()] == [name]
        for _ in range(2):  # Firefox allows one session at a time: every detach must end it cleanly
            async with Browser.attach(name) as attached:
                tabs = await attached.tabs()
                urls = [(await tab.info())["url"] for tab in tabs]
                assert site.url + "/set-cookie" in urls
                tab = tabs[urls.index(site.url + "/set-cookie")]
                assert await tab.evaluate("document.cookie") == "session=abc123"
        with pytest.raises(BrowserError, match="already running"):
            await Browser(full_browser_name).start_instance(name)
    finally:
        instances.stop(name)
    assert not instance.alive and instances.list_all() == []
    assert not Path(instance.run_dir).exists()
    with pytest.raises(BrowserError, match="No instance"):
        Browser.attach(name)


async def test_lightpanda_cannot_be_a_named_instance(monkeypatch, tmp_path):
    from browsers import require_browser

    require_browser("lightpanda")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(UnsupportedError):
        await Browser("lightpanda").start_instance("panda")


async def test_stealth_default_hides_automation_and_can_be_disabled(full_browser_name, site):
    probe = "({webdriver: navigator.webdriver, own: Object.getOwnPropertyNames(navigator).includes('webdriver')})"
    async with Browser(full_browser_name) as browser:
        tab = await browser.new_tab()
        headers = await echoed_headers(tab, site)
        assert await tab.evaluate(probe) == {"webdriver": False, "own": False}
        assert "Headless" not in headers["user-agent"] and "Headless" not in await tab.evaluate("navigator.userAgent")
        if full_browser_name == "chromium":
            assert "Chromium" in headers.get("sec-ch-ua", "") and "Headless" not in headers["sec-ch-ua"]
            assert await tab.evaluate("navigator.userAgentData.brands.length") > 0
    async with Browser(full_browser_name, stealth=False) as browser:
        tab = await browser.new_tab(site.url + "/final")
        assert (await tab.evaluate(probe))["webdriver"] is True


async def test_browser_leaves_no_process_no_core_file_and_no_temp_dir(browser_name, site, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "/")  # the classic container setup that made Chromium crash and dump core
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/final")
        assert await tab.title() == "Final"
        process = browser._process
        pid, run_dir = process.pid, process.run_dir
        group = os.getpgid(pid)
        assert group == pid and group != os.getpgid(0)
    assert not group_is_running(group)  # zombies may linger when PID 1 does not reap, live processes may not
    assert not run_dir.exists()
    assert list(tmp_path.glob("core*")) == []


async def test_connect_drives_a_browser_that_is_already_running(full_browser_name, site):
    from crau.browser.launch import BrowserProcess, LaunchOptions

    process = await BrowserProcess(LaunchOptions(browser=full_browser_name)).start()
    try:
        for _ in range(2):  # disconnecting must leave the browser usable for the next client
            async with Browser.connect(process.endpoint, full_browser_name) as browser:
                assert browser.version
                tab = await browser.new_tab(site.url + "/final")
                assert await tab.title() == "Final"
                await tab.close()
        assert group_is_running(process.pid)
    finally:
        process.stop()


async def test_settle_reports_scrolling_and_pending_requests(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/slow")
        stats = await tab.settle(10, idle=0.3, minimum=0.5)
        assert await tab.evaluate("document.querySelector('#out').textContent") == "slow-done"
        assert stats["pending"] == 0 and stats["scroll_steps"] == 0 and 0.5 <= stats["settle_seconds"] < 10
        await tab.goto(site.url + "/")
        scrolled = await tab.settle(10, scroll=True)
        if browser_name == "lightpanda":  # nothing to scroll without layout: it must not run until the timeout
            assert scrolled["scroll_steps"] == 0 and scrolled["settle_seconds"] < 5
        else:
            assert scrolled["scroll_steps"] >= 2


async def test_explicit_debugging_port_is_used(site):
    import socket

    from browsers import require_browser

    require_browser("chromium")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    async with Browser("chromium", port=port) as browser:
        assert f":{port}/" in browser._process.endpoint
        tab = await browser.new_tab(site.url + "/final", apply_viewport=False)
        assert await tab.title() == "Final"
