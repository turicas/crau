from __future__ import annotations

from pathlib import Path
from typing import Any

from crau.fetchers.browser import BrowserFetcher


class LightpandaFetcher(BrowserFetcher):
    """High-performance headless browser fetcher powered by Lightpanda (CDP, no rendering, no images)."""

    browser_name = "lightpanda"

    def __init__(
        self,
        binary_path: str | Path = "lightpanda",
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float = 30.0,
        user_agent: str | None = None,
        **settle_options: Any,
    ):
        super().__init__(
            binary_path=binary_path, host=host, port=port, timeout=timeout, user_agent=user_agent, **settle_options
        )
