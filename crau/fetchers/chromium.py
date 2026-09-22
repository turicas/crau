from __future__ import annotations

from crau.fetchers.browser import BrowserFetcher


class ChromiumFetcher(BrowserFetcher):
    """Headless Chromium / Google Chrome fetcher communicating via CDP.

    The browser is started by ``crau.browser`` on a port it picks itself, with a writable HOME,
    no automation fingerprints (``navigator.webdriver`` false, no ``HeadlessChrome`` in the
    User-Agent, coherent client hints) and its output kept for error messages.
    """

    browser_name = "chromium"
