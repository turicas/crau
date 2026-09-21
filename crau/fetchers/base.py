from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

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


# Returns JSON text: CDP and BiDi serialize objects differently, strings travel the same
SCROLL_STEP_SCRIPT = """
(() => {
  const before = window.scrollY;
  const height = document.documentElement.scrollHeight;
  window.scrollBy(0, window.innerHeight);
  return JSON.stringify({moved: window.scrollY !== before, height: height});
})()
"""


class NetworkActivity:
    """Bookkeeping for the settle phase: which requests are in flight and when the last one happened."""

    def __init__(self):
        self.pending: set[str] = set()
        self.last_activity: float = time.perf_counter()

    def started(self, request_id: str) -> None:
        self.pending.add(request_id)
        self.last_activity = time.perf_counter()

    def finished(self, request_id: str) -> None:
        self.pending.discard(request_id)
        self.last_activity = time.perf_counter()

    def reset(self) -> None:
        self.pending.clear()
        self.last_activity = time.perf_counter()

    def quiet_for(self) -> float:
        return time.perf_counter() - self.last_activity


async def settle_page(
    evaluate: Callable[[str], Awaitable[Any]],
    activity: NetworkActivity,
    timeout: float,
    idle: float = 0.5,
    min_time: float = 2.0,
    auto_scroll: bool = True,
    poll: float = 0.1,
) -> dict[str, Any]:
    """Wait after the load event for late requests (XHR, lazy images, infinite scroll).

    Scrolls one viewport at a time until the page stops growing, and returns once at
    least `min_time` has passed (timers firing shortly after load) and no request has
    started or finished for `idle` seconds with nothing in flight, or when `timeout` is
    reached (the hard cap for pages that never go quiet). `evaluate` runs a JS
    expression in the page and returns its value.
    """
    started = time.perf_counter()
    scrolls = 0
    last_height = None
    at_bottom = not auto_scroll
    while time.perf_counter() - started < timeout:
        if not at_bottom:
            raw = await evaluate(SCROLL_STEP_SCRIPT)
            result = json.loads(raw) if isinstance(raw, str) else {}
            moved = bool(result.get("moved"))
            height = result.get("height") if result else None
            scrolls += 1
            if not moved and height == last_height:
                at_bottom = True
            last_height = height
        elapsed = time.perf_counter() - started
        if at_bottom and elapsed >= min_time and not activity.pending and activity.quiet_for() >= idle:
            break
        await asyncio.sleep(poll)
    return {"settle_seconds": time.perf_counter() - started, "scroll_steps": scrolls, "pending": len(activity.pending)}


def create_browser_log(prefix: str) -> Path:
    """Temporary file that receives the browser's stdout and stderr.

    Discarding them hides the only explanation a browser gives when it fails to start
    (missing shared library, unusable profile, port already taken).
    """
    handle, name = tempfile.mkstemp(prefix=f"crau-{prefix}-", suffix=".log")
    os.close(handle)
    return Path(name)


def browser_port_from_log(log_path: Path | None, pattern: str) -> int | None:
    """Port announced by a browser started on port 0; `pattern` must capture the port in group 1.

    Asking the OS for a free port, closing it and handing the number to the browser is a race:
    another process (or another fetcher) can take the port in between.
    """
    if log_path is None or not log_path.exists():
        return None
    match = re.search(pattern, log_path.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


def browser_log_tail(log_path: Path | None, limit: int = 2000) -> str:
    """Last `limit` characters of the browser log, formatted to be appended to an error message."""
    if log_path is None or not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    return f". Browser output:\n{text[-limit:]}" if text else ""
