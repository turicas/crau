from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class AutoThrottle:
    """Adaptive download delay controller based on recent server latencies."""

    def __init__(
        self,
        enabled: bool = True,
        min_delay: float = 0.1,
        max_delay: float = 10.0,
        target_concurrency: float = 1.0,
    ):
        self.enabled = enabled
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.target_concurrency = target_concurrency

        self._domain_latencies: dict[str, list[float]] = defaultdict(list)
        self._last_request_time: dict[str, float] = {}

    def record_latency(self, domain: str, latency: float) -> None:
        if not self.enabled:
            return
        latencies = self._domain_latencies[domain]
        latencies.append(latency)
        if len(latencies) > 20:
            latencies.pop(0)

    def get_delay(self, domain: str) -> float:
        if not self.enabled:
            return 0.0

        latencies = self._domain_latencies.get(domain)
        if not latencies:
            return self.min_delay

        avg_latency = sum(latencies) / len(latencies)
        target_delay = avg_latency / max(self.target_concurrency, 0.1)
        return max(self.min_delay, min(self.max_delay, target_delay))

    async def wait_throttle(self, domain: str) -> None:
        if not self.enabled:
            return

        now = time.perf_counter()
        last_time = self._last_request_time.get(domain, 0.0)
        delay = self.get_delay(domain)
        elapsed = now - last_time

        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)

        self._last_request_time[domain] = time.perf_counter()
