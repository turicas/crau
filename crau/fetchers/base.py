from __future__ import annotations

from typing import Any, Protocol

from crau.models import CrawlResult


class Fetcher(Protocol):
    """Protocol for pluggable web fetchers (HTTP, CDP, BiDi, etc.)."""

    async def __aenter__(self) -> "Fetcher":
        ...

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        ...

    async def fetch(self, url: str, **kwargs: Any) -> CrawlResult:
        """Fetch URL and return a CrawlResult with transactions and rendered page."""
        ...
