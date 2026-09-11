from __future__ import annotations

import asyncio
import base64
import datetime
import json
import time
from typing import Any

from crau.fetchers.base import Fetcher
from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore


class CdpBrowserFetcher(Fetcher):
    """Generic browser fetcher communicating via Chrome DevTools Protocol (CDP)."""

    def __init__(
        self,
        endpoint_url: str,
        timeout: float = 30.0,
        user_agent: str | None = None,
    ):
        if websockets is None:
            raise ImportError(
                "The 'websockets' library is required for CDP browser fetchers. "
                "Install it via 'pip install crau[browser]'."
            )
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self.user_agent = user_agent
        self._ws: Any = None
        self._msg_id = 0

    async def __aenter__(self) -> "CdpBrowserFetcher":
        self._ws = await websockets.connect(self.endpoint_url)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _send_command(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._msg_id += 1
        cmd_id = self._msg_id
        payload = {"id": cmd_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        return cmd_id

    async def fetch(
        self,
        url: str,
        wait_until: str = "load",
        wait_for_selector: str | None = None,
        evaluate_js: str | None = None,
        **kwargs: Any,
    ) -> CrawlResult:
        if not self._ws:
            raise RuntimeError("Fetcher connection is not open. Use 'async with fetcher:'")

        start_time = time.perf_counter()
        req_records: dict[str, dict] = {}
        resp_records: dict[str, dict] = {}
        pending_commands: dict[int, asyncio.Future] = {}
        load_event = asyncio.Event()

        # Enable necessary CDP domains
        await self._send_command("Network.enable")
        await self._send_command("Page.enable")
        await self._send_command("Runtime.enable")

        # Navigate
        nav_cmd_id = await self._send_command("Page.navigate", {"url": url})

        async def _reader_loop():
            async for raw_msg in self._ws:
                data = json.loads(raw_msg)
                msg_id = data.get("id")
                if msg_id in pending_commands:
                    fut = pending_commands.pop(msg_id)
                    if not fut.done():
                        fut.set_result(data.get("result", {}))
                    continue

                method = data.get("method")
                params = data.get("params", {})

                if method == "Network.requestWillBeSent":
                    req_id = params.get("requestId")
                    if req_id:
                        req_records[req_id] = params.get("request", {})
                elif method == "Network.responseReceived":
                    req_id = params.get("requestId")
                    if req_id:
                        resp_records[req_id] = params.get("response", {})
                elif method == "Page.loadEventFired":
                    load_event.set()

        reader_task = asyncio.create_task(_reader_loop())

        try:
            # Wait for page load
            await asyncio.wait_for(load_event.wait(), timeout=self.timeout)

            # Optional: evaluate custom JS
            if evaluate_js:
                eval_fut = asyncio.get_running_loop().create_future()
                eval_id = await self._send_command(
                    "Runtime.evaluate", {"expression": evaluate_js, "returnByValue": True}
                )
                pending_commands[eval_id] = eval_fut
                await asyncio.wait_for(eval_fut, timeout=self.timeout)

            # Get rendered HTML
            dom_fut = asyncio.get_running_loop().create_future()
            dom_id = await self._send_command(
                "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
            )
            pending_commands[dom_id] = dom_fut
            dom_result = await asyncio.wait_for(dom_fut, timeout=self.timeout)
            rendered_html = (
                dom_result.get("result", {}).get("value")
                or dom_result.get("result", {}).get("description")
                or ""
            )

            # Retrieve response bodies for captured network requests
            transactions: list[NetworkTransaction] = []
            for req_id, req_data in req_records.items():
                resp_data = resp_records.get(req_id, {})
                body_bytes = b""

                try:
                    body_fut = asyncio.get_running_loop().create_future()
                    cmd_id = await self._send_command(
                        "Network.getResponseBody", {"requestId": req_id}
                    )
                    pending_commands[cmd_id] = body_fut
                    body_res = await asyncio.wait_for(body_fut, timeout=5.0)
                    raw_body = body_res.get("body", "")
                    if body_res.get("base64Encoded"):
                        body_bytes = base64.b64decode(raw_body)
                    else:
                        body_bytes = raw_body.encode("utf-8")
                except Exception:
                    body_bytes = b""

                # Convert headers
                req_headers: list[tuple[bytes, bytes]] = [
                    (k.encode("latin1"), str(v).encode("latin1"))
                    for k, v in req_data.get("headers", {}).items()
                ]
                resp_headers: list[tuple[bytes, bytes]] = [
                    (k.encode("latin1"), str(v).encode("latin1"))
                    for k, v in resp_data.get("headers", {}).items()
                ]

                now = datetime.datetime.now(datetime.timezone.utc)
                raw_req = RawRequest(
                    url=req_data.get("url", url),
                    method=req_data.get("method", "GET"),
                    raw_headers=req_headers,
                    raw_body=(req_data.get("postData", "") or "").encode("utf-8"),
                    timestamp=now,
                )
                raw_resp = RawResponse(
                    status_code=resp_data.get("status", 200),
                    reason_phrase=resp_data.get("statusText", "OK"),
                    http_version=resp_data.get("protocol", "HTTP/1.1").upper(),
                    raw_headers=resp_headers,
                    raw_body=body_bytes,
                    timestamp=now,
                )
                transactions.append(
                    NetworkTransaction(
                        request=raw_req,
                        response=raw_resp,
                        duration_seconds=time.perf_counter() - start_time,
                    )
                )

            page = Page(
                url=url,
                status_code=transactions[0].response.status_code if transactions else 200,
                content=rendered_html,
                raw_body=transactions[0].response.raw_body if transactions else b"",
                transactions=transactions,
                timing={"total": time.perf_counter() - start_time},
            )

            return CrawlResult(transactions=transactions, page=page)
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
