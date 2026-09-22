from __future__ import annotations

import json

import pytest
from crau.browser import Browser, BrowserError, WaitTimeout


async def test_fill_and_click_drive_a_form_like_a_user(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/form")
        await tab.fill("#user", "alice")
        await tab.fill("label=Password", "s3cret")
        await tab.fill("label=Colour", "Green")
        await tab.fill("input[name=remember]", True)
        assert await tab.evaluate("document.querySelector('#mirror').textContent") == "alice"  # input event fired
        await tab.click("text=Sign in")
        await tab.wait(js="document.title === 'signed-in'", timeout=10)
        result = json.loads(await tab.evaluate("document.querySelector('#result').textContent"))
        assert {key: result[key] for key in ("user", "password", "color", "remember")} == {
            "user": "alice",
            "password": "s3cret",
            "color": "g",
            "remember": True,
        }
        if browser_name != "lightpanda":
            assert result["trusted"] is True  # real mouse events, not element.click()


async def test_click_scrolls_a_far_element_into_view(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/form")
        await tab.click("#far")
        await tab.wait(js="document.title === 'far-clicked'", timeout=5)


async def test_click_follows_a_link_found_by_its_text(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/")
        await tab.click("text=Form page")
        await tab.wait(js="location.pathname === '/form'", timeout=10)


async def test_type_and_press_send_real_keystrokes(full_browser_name, site):
    async with Browser(full_browser_name) as browser:
        tab = await browser.new_tab(site.url + "/form")
        await tab.type("label=Search term", "open data")
        await tab.press("Enter")
        await tab.wait(js="document.title === 'searched:open data'", timeout=5)


async def test_acting_on_a_missing_element_times_out_and_bad_option_fails(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/form")
        with pytest.raises(WaitTimeout):
            await tab.click("#does-not-exist", timeout=0.5)
        with pytest.raises(BrowserError, match="Purple"):
            await tab.fill("#color", "Purple")
        assert await tab.exists("#user") is True and await tab.exists("text=No such text") is False


async def test_outline_maps_the_page_with_usable_selectors(browser_name, site):
    async with Browser(browser_name) as browser:
        tab = await browser.new_tab(site.url + "/form")
        outline = await tab.outline()
        assert outline["title"] == "Form" and outline["url"].endswith("/form")
        by_name = {(item["role"], item["name"]): item for item in outline["items"]}
        assert ("heading", "Sign in") in by_name and by_name[("heading", "Sign in")]["level"] == 1
        assert by_name[("textbox", "User name")]["selector"] == "#user"
        assert ("combobox", "Colour") in by_name and ("checkbox", "Remember me") in by_name
        password = next(item for item in outline["items"] if item.get("type") == "password")
        assert "value" not in password
        await tab.fill(password["selector"], "via-outline")
        assert await tab.evaluate("document.querySelector('[name=password]').value") == "via-outline"
        await tab.click(by_name[("button", "Far away")]["selector"])
        await tab.wait(js="document.title === 'far-clicked'", timeout=5)
