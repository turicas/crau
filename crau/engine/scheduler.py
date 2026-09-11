from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urldefrag


@dataclass(order=True)
class PriorityItem:
    priority: int
    counter: int
    item: RequestItem = field(compare=False)


@dataclass
class RequestItem:
    url: str
    depth: int = 0
    callback: Any = None
    meta: dict[str, Any] = field(default_factory=dict)
    retries: int = 0


class Scheduler:
    """Async priority queue and domain-aware concurrency limiter."""

    def __init__(
        self,
        max_concurrent_requests: int = 16,
        max_concurrent_per_domain: int = 4,
    ):
        self.max_concurrent_requests = max_concurrent_requests
        self.max_concurrent_per_domain = max_concurrent_per_domain

        self._queue: asyncio.PriorityQueue[PriorityItem] = asyncio.PriorityQueue()
        self._seen_urls: set[str] = set()
        self._counter = 0

        self._global_sem = asyncio.Semaphore(max_concurrent_requests)
        self._domain_sems: dict[str, asyncio.Semaphore] = {}

    def _normalize_url(self, url: str) -> str:
        cleaned, _ = urldefrag(url)
        return cleaned

    async def enqueue(
        self,
        url: str,
        depth: int = 0,
        callback: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        norm_url = self._normalize_url(url)
        if norm_url in self._seen_urls:
            return False

        self._seen_urls.add(norm_url)
        item = RequestItem(
            url=norm_url,
            depth=depth,
            callback=callback,
            meta=meta or {},
        )
        self._counter += 1
        await self._queue.put(PriorityItem(priority=depth, counter=self._counter, item=item))
        return True

    async def dequeue(self) -> RequestItem:
        priority_item = await self._queue.get()
        return priority_item.item

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def task_done(self) -> None:
        self._queue.task_done()

    @asynccontextmanager
    async def limit(self, domain: str) -> AsyncIterator[None]:
        if domain not in self._domain_sems:
            self._domain_sems[domain] = asyncio.Semaphore(self.max_concurrent_per_domain)

        domain_sem = self._domain_sems[domain]
        async with self._global_sem:
            async with domain_sem:
                yield
