"""Per-tab network bookkeeping shared by both protocols: in-flight requests, the main document, recorded traffic."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NetworkRecord:
    key: str
    url: str
    method: str = "GET"
    request_headers: list[tuple[str, str]] = field(default_factory=list)
    post_data: str | None = None
    has_post_data: bool = False
    started: float | None = None
    resource_type: str = ""
    is_navigation: bool = False
    status: int = 0
    status_text: str = ""
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    mime_type: str = ""
    protocol: str = ""
    remote_ip: str = ""
    remote_port: int = 0
    wait_ms: float = -1
    duration_ms: float = -1
    encoded_size: int = 0
    body: bytes | None = None
    error: str | None = None
    finished: bool = False


class NetworkMonitor:
    """Consumes the normalized events a backend tab emits.

    Always tracks what is in flight (for "network idle" waits) and the navigation records (for the main document's
    status). While `recording`, it also keeps every record and downloads bodies the moment each request finishes:
    Chromium drops them as soon as the tab navigates elsewhere.
    """

    def __init__(self, backend_tab) -> None:
        self._backend = backend_tab
        self.records: dict[str, NetworkRecord] = {}
        self.inflight: set[str] = set()
        self.last_activity = time.perf_counter()
        self.recording = False
        self.bodies_available = True
        self._early_headers: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._body_tasks: set[asyncio.Task] = set()

    def reset_navigation(self) -> None:
        if not self.recording:
            self.records.clear()
        self.inflight.clear()
        self.last_activity = time.perf_counter()

    def quiet_for(self) -> float:
        return time.perf_counter() - self.last_activity

    def main_document(self, requested_url: str) -> NetworkRecord | None:
        """Final hop of the latest navigation; falls back to the first document-like record."""
        answered = [record for record in self.records.values() if record.status]
        navigations = [record for record in answered if record.is_navigation]
        if navigations:
            return navigations[-1]
        documents = [record for record in answered if record.resource_type.lower() == "document"]
        same_url = [record for record in answered if record.url.split("#")[0] == requested_url.split("#")[0]]
        candidates = documents or same_url or answered
        return candidates[0] if candidates else None

    def handle(self, event: dict[str, Any]) -> None:
        kind, key = event["kind"], event["id"]
        self.last_activity = time.perf_counter()
        if kind == "request":
            record = NetworkRecord(
                key=key,
                url=event["url"],
                method=event["method"],
                request_headers=self._early_headers.pop((key, "request"), None) or event["headers"],
                post_data=event["post_data"],
                has_post_data=event["has_post_data"],
                started=event["time"] or time.time(),
                resource_type=event["resource_type"],
                is_navigation=event["is_navigation"],
            )
            self.records[key] = record
            self.inflight.add(key)
            return
        record = self.records.get(key)
        if kind in ("request_headers", "response_headers"):
            # headers as really sent/received (cookies included); may arrive before the record they belong to
            side = kind.split("_")[0]
            if not event["headers"]:
                return
            if side == "request" and record is not None:
                record.request_headers = event["headers"]
            elif side == "response" and record is not None and record.status:
                record.response_headers = event["headers"]
            else:
                self._early_headers[(key, side)] = event["headers"]
            return
        if record is None:
            return
        if kind == "response":
            record.status, record.status_text = event["status"], event["status_text"]
            record.response_headers = self._early_headers.pop((key, "response"), None) or event["headers"]
            record.mime_type, record.protocol = event["mime_type"], event["protocol"]
            record.remote_ip, record.wait_ms = event["remote_ip"], event["wait_ms"]
            record.remote_port, record.duration_ms = event.get("remote_port", 0), event.get("duration_ms", -1)
        elif kind == "finished":
            record.finished, record.encoded_size = True, event["encoded_size"]
            self.inflight.discard(key)
            if self.recording:
                self._spawn(self._fetch_bodies(record))
        elif kind == "failed":
            record.finished, record.error = True, event["error"]
            self.inflight.discard(key)

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._body_tasks.add(task)
        task.add_done_callback(self._body_tasks.discard)

    async def _fetch_bodies(self, record: NetworkRecord) -> None:
        try:
            if self.bodies_available and 200 <= record.status and record.status not in (204, 304):
                record.body = await self._backend.response_body(record.key)
            if record.has_post_data and record.post_data is None:
                record.post_data = await self._backend.request_body(record.key)
        except Exception:  # the tab or the connection went away; the entry simply stays without a body
            pass

    async def start_recording(self) -> None:
        self.recording = True
        self.records.clear()
        self.bodies_available = await self._backend.start_body_capture()

    async def stop_recording(self) -> list[NetworkRecord]:
        if self._body_tasks:
            await asyncio.gather(*list(self._body_tasks), return_exceptions=True)
        self.recording = False
        return sorted(self.records.values(), key=lambda record: record.started or 0)
