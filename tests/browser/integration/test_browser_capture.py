from __future__ import annotations

import struct

import pytest
from crau.browser import Browser, UnsupportedError


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


async def test_screenshot_viewport_full_page_element_and_jpeg(full_browser_name, site, tmp_path):
    async with Browser(full_browser_name, viewport=(800, 600)) as browser:
        tab = await browser.new_tab(site.url + "/")
        viewport = png_size(await tab.screenshot())
        assert viewport == (800, 600)
        page_height = await tab.evaluate("document.documentElement.scrollHeight")
        full = png_size(await tab.screenshot(tmp_path / "shots" / "full.png", full_page=True))
        assert 780 <= full[0] <= 800 and abs(full[1] - page_height) <= 2 and full[1] > 2000
        assert (tmp_path / "shots" / "full.png").read_bytes()[:4] == b"\x89PNG"
        element = png_size(await tab.screenshot(selector="#bottom"))
        assert element[1] < 100 and element[0] <= 800
        jpeg = await tab.screenshot(tmp_path / "page.jpg", quality=60)
        assert jpeg[:3] == b"\xff\xd8\xff" and (tmp_path / "page.jpg").stat().st_size == len(jpeg)


async def test_lightpanda_screenshot_is_a_png(site):
    from browsers import require_browser

    require_browser("lightpanda")
    async with Browser("lightpanda") as browser:
        tab = await browser.new_tab(site.url + "/")
        assert (await tab.screenshot(full_page=True))[:8] == b"\x89PNG\r\n\x1a\n"


async def test_pdf_is_written(full_browser_name, site, tmp_path):
    async with Browser(full_browser_name) as browser:
        tab = await browser.new_tab(site.url + "/")
        data = await tab.pdf(tmp_path / "out" / "page.pdf", landscape=True)
        assert data[:5] == b"%PDF-" and (tmp_path / "out" / "page.pdf").read_bytes() == data


async def test_pdf_header_and_footer_templates_only_on_chromium(full_browser_name, site):
    footer = "<div style='font-size:9pt'><span class='pageNumber'></span>/<span class='totalPages'></span></div>"
    async with Browser(full_browser_name) as browser:
        tab = await browser.new_tab(site.url + "/")
        if full_browser_name == "chromium":
            assert (await tab.pdf(footer=footer))[:5] == b"%PDF-"
        else:
            with pytest.raises(UnsupportedError):
                await tab.pdf(footer=footer)


async def test_lightpanda_cannot_print(site):
    from browsers import require_browser

    require_browser("lightpanda")
    async with Browser("lightpanda") as browser:
        tab = await browser.new_tab(site.url + "/final")
        with pytest.raises(UnsupportedError):
            await tab.pdf()
