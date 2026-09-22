"""Every raw-protocol recipe in references/cdp.md and references/bidi.md is exercised here, so the docs cannot rot."""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import tempfile

from crau.browser import Browser
from crau.browser.launch import find_binary
from browsers import require_browser


async def test_cdp_accessibility_tree(site):
    require_browser("chromium")
    async with Browser("chromium") as browser:
        tab = await browser.new_tab(site.url + "/form")
        nodes = (await tab.cdp("Accessibility.getFullAXTree"))["nodes"]
        named = {
            (node["role"]["value"], node.get("name", {}).get("value")) for node in nodes if not node.get("ignored")
        }
        assert ("button", "Sign in") in named and ("textbox", "User name") in named


async def test_cdp_evaluate_inside_an_iframe(site):
    require_browser("chromium")
    async with Browser("chromium") as browser:
        tab = await browser.new_tab(site.url + "/")
        tree = (await tab.cdp("Page.getFrameTree"))["frameTree"]
        frame_id = tree["childFrames"][0]["frame"]["id"]
        world = await tab.cdp("Page.createIsolatedWorld", {"frameId": frame_id})
        result = await tab.cdp(
            "Runtime.evaluate",
            {"expression": "document.querySelector('button').textContent", "contextId": world["executionContextId"],
             "returnByValue": True},
        )  # fmt: skip
        assert result["result"]["value"] == "inside iframe"


async def test_cdp_block_requests_by_url_pattern(site):
    require_browser("chromium")
    async with Browser("chromium") as browser:
        tab = await browser.new_tab()
        await tab.cdp("Network.setBlockedURLs", {"urls": ["*/api/echo*"]})
        await tab.goto(site.url + "/", wait_idle=True)
    assert [request for request in site.requests if request[0] == "POST"] == []


async def test_cdp_downloads_go_to_a_directory(site, tmp_path):
    require_browser("chromium")
    async with Browser("chromium") as browser:
        tab = await browser.new_tab(site.url + "/final")
        await tab.cdp("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(tmp_path)})
        await tab.evaluate(
            "const a = document.createElement('a'); a.href = '/binary'; a.download = 'data.bin';"
            " document.body.appendChild(a); a.click(); true"
        )
        for _ in range(100):
            if (tmp_path / "data.bin").exists():
                break
            await asyncio.sleep(0.1)
    assert (tmp_path / "data.bin").read_bytes() == bytes(range(256))


def test_cdp_over_a_pipe_needs_no_port():
    """--remote-debugging-pipe: the browser reads commands from fd 3 and writes replies to fd 4, NUL-terminated."""
    require_browser("chromium")
    to_browser_read, to_browser_write = os.pipe()
    from_browser_read, from_browser_write = os.pipe()
    profile = tempfile.mkdtemp(prefix="pipe-profile-")

    def wire_descriptors() -> None:
        # pipe fds are close-on-exec and dup2(n, n) keeps that flag: go through high numbers first
        high_read, high_write = os.dup(to_browser_read), os.dup(from_browser_write)
        os.dup2(high_read, 3, inheritable=True)
        os.dup2(high_write, 4, inheritable=True)

    command = [str(find_binary("chromium")), "--headless=new", "--no-sandbox", "--remote-debugging-pipe",
               f"--user-data-dir={profile}", "about:blank"]  # fmt: skip
    process = subprocess.Popen(command, preexec_fn=wire_descriptors, close_fds=False, stderr=subprocess.DEVNULL)
    try:
        os.write(to_browser_write, json.dumps({"id": 1, "method": "Browser.getVersion"}).encode() + b"\0")
        buffer = b""
        while b"\0" not in buffer:
            assert select.select([from_browser_read], [], [], 30)[0], "no reply on the pipe"
            buffer += os.read(from_browser_read, 1 << 16)
        reply = json.loads(buffer.split(b"\0")[0])
        assert reply["id"] == 1 and "product" in reply["result"]
    finally:
        process.kill()
        process.wait(timeout=10)


async def test_bidi_locate_nodes_by_accessibility_and_evaluate_in_iframe(site):
    require_browser("firefox")
    async with Browser("firefox") as browser:
        tab = await browser.new_tab(site.url + "/")
        located = await tab.bidi(
            "browsingContext.locateNodes",
            {"context": tab.id, "locator": {"type": "accessibility", "value": {"role": "link", "name": "Form page"}}},
        )
        assert len(located["nodes"]) == 1 and located["nodes"][0]["value"]["attributes"]["id"] == "to-form"
        tree = await tab.bidi("browsingContext.getTree", {"root": tab.id})
        child = tree["contexts"][0]["children"][0]["context"]
        inside = await tab.bidi(
            "script.evaluate",
            {"expression": "document.querySelector('button').textContent", "target": {"context": child},
             "awaitPromise": False},
        )  # fmt: skip
        assert inside["result"] == {"type": "string", "value": "inside iframe"}


async def test_bidi_block_requests_with_an_intercept(site):
    require_browser("firefox")
    async with Browser("firefox") as browser:
        tab = await browser.new_tab()
        connection = browser._backend.connection
        await tab.bidi("session.subscribe", {"events": ["network.beforeRequestSent"]})
        await tab.bidi(
            "network.addIntercept",
            {"phases": ["beforeRequestSent"], "urlPatterns": [{"type": "pattern", "pathname": "/api/echo"}]},
        )

        def fail_blocked(event: dict) -> None:
            if event["method"] == "network.beforeRequestSent" and event["params"].get("isBlocked"):
                request_id = event["params"]["request"]["request"]
                asyncio.ensure_future(connection.call("network.failRequest", {"request": request_id}))

        connection.add_listener(fail_blocked)
        await tab.goto(site.url + "/", wait_idle=True)
    assert [request for request in site.requests if request[0] == "POST"] == []


async def test_bidi_preload_script_runs_before_page_scripts(site):
    require_browser("firefox")
    async with Browser("firefox") as browser:
        tab = await browser.new_tab()
        await tab.bidi("script.addPreloadScript", {"functionDeclaration": "() => { window.injectedEarly = 42; }"})
        await tab.goto(site.url + "/final")
        assert await tab.evaluate("window.injectedEarly") == 42
