from __future__ import annotations

from crau.fetchers.browser import BrowserFetcher


class FirefoxFetcher(BrowserFetcher):
    """Headless Firefox fetcher communicating via W3C WebDriver BiDi (CDP was removed in Firefox 141).

    Request and response bodies come from BiDi network data collectors, which exist in current
    Firefox releases but not in ESR 140: there, transactions are recorded with empty bodies.
    Requests whose body Firefox cannot hand over (data: URLs, some cached resources) are
    recorded with an empty body too.
    """

    browser_name = "firefox"
