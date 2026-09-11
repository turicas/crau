from __future__ import annotations

from typing import Protocol

from crau.models import NetworkTransaction


class CacheBackend(Protocol):
    """Protocol for crawler request/response cache storage."""

    async def __aenter__(self) -> "CacheBackend":
        ...

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        ...

    async def get(self, url: str) -> list[NetworkTransaction] | None:
        """Retrieve cached transactions for a URL if valid."""
        ...

    async def store(self, url: str, transactions: list[NetworkTransaction]) -> None:
        """Store transactions associated with a URL in the cache."""
        ...

    async def close(self) -> None:
        """Close cache resources."""
        ...
