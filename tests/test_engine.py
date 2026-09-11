import asyncio
import time
import pytest

from crau.engine.scheduler import RequestItem, Scheduler
from crau.engine.throttle import AutoThrottle


@pytest.mark.asyncio
async def test_auto_throttle_adjusts_delay():
    throttle = AutoThrottle(enabled=True, min_delay=0.01, max_delay=1.0)
    domain = "example.com"

    # Initially at min_delay
    delay = throttle.get_delay(domain)
    assert delay == 0.01

    # Record higher latency (0.5s)
    throttle.record_latency(domain, 0.5)
    delay = throttle.get_delay(domain)
    assert delay >= 0.5

    # Disabled throttle returns 0
    disabled_throttle = AutoThrottle(enabled=False)
    assert disabled_throttle.get_delay(domain) == 0.0


@pytest.mark.asyncio
async def test_scheduler_deduplication_and_priority():
    scheduler = Scheduler(max_concurrent_requests=10, max_concurrent_per_domain=2)

    added1 = await scheduler.enqueue("https://example.com/page1", depth=1)
    added2 = await scheduler.enqueue("https://example.com/page1#fragment", depth=2)
    added3 = await scheduler.enqueue("https://example.com/root", depth=0)

    assert added1 is True
    assert added2 is False  # Duplicate after stripping fragment
    assert added3 is True

    # Priority queue: depth=0 must come before depth=1
    first = await scheduler.dequeue()
    assert first.url == "https://example.com/root"
    assert first.depth == 0

    second = await scheduler.dequeue()
    assert second.url == "https://example.com/page1"
    assert second.depth == 1


@pytest.mark.asyncio
async def test_scheduler_domain_concurrency():
    scheduler = Scheduler(max_concurrent_requests=5, max_concurrent_per_domain=1)
    active = 0
    max_observed = 0

    async def worker():
        nonlocal active, max_observed
        async with scheduler.limit("example.com"):
            active += 1
            max_observed = max(max_observed, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(worker(), worker(), worker())
    assert max_observed == 1
