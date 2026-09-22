from __future__ import annotations

from typing import Any

from crau.fetchers.browser import BrowserFetcher


class CdpBrowserFetcher(BrowserFetcher):
    """Fetcher for a browser that is already running and speaks the Chrome DevTools Protocol.

    ``endpoint_url`` is the browser-level websocket (the ``webSocketDebuggerUrl`` of
    ``/json/version``). The fetcher reuses a page that is already open (or creates one) and
    leaves the browser running when it exits.
    """

    def __init__(self, endpoint_url: str, timeout: float = 30.0, user_agent: str | None = None, **settle_options: Any):
        super().__init__(endpoint_url=endpoint_url, timeout=timeout, user_agent=user_agent, **settle_options)

    async def send(self, method: str, params: dict[str, Any] | None = None, session: bool = True) -> dict[str, Any]:
        """Send a raw CDP command: to the fetcher's page (default) or, with ``session=False``, to the browser."""
        if self.tab is None:
            raise RuntimeError("Fetcher connection is not open. Use 'async with fetcher:'")
        if session:
            return await self.tab.cdp(method, params)
        return await self.browser._backend.connection.call(method, params)
