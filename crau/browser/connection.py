"""JSON command/event connections for the two automation protocols: CDP (Chromium, Lightpanda) and BiDi (Firefox).

One background task reads the socket, resolves the future of the command each reply belongs to and hands events to the
registered listeners, so commands can be issued concurrently and events are never lost while a command is pending.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .errors import BrowserError, ProtocolError
from .websocket import WebSocket, WebSocketError

EventListener = Callable[[dict[str, Any]], None]


class Connection:
    """Behaviour shared by both protocols; subclasses define how replies and events look."""

    def __init__(self, websocket: WebSocket, timeout: float = 30.0) -> None:
        self._websocket = websocket
        self.timeout = timeout
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}
        self._listeners: list[EventListener] = []
        self._reader_task: asyncio.Task | None = None
        self._closed_reason: str | None = None

    @classmethod
    async def connect(cls, url: str, timeout: float = 30.0):
        connection = cls(await WebSocket.connect(url, timeout=min(timeout, 15.0)), timeout=timeout)
        connection._reader_task = asyncio.create_task(connection._read_loop(), name="crau-reader")
        return connection

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: EventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def wait_for_event(self, predicate: Callable[[dict[str, Any]], bool], timeout: float) -> dict[str, Any]:
        """First future event for which `predicate(event)` is true (raises `asyncio.TimeoutError`)."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        def listener(event: dict[str, Any]) -> None:
            if not future.done() and predicate(event):
                future.set_result(event)

        self.add_listener(listener)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.remove_listener(listener)

    async def _send(self, method: str, message: dict[str, Any], timeout: float | None) -> Any:
        if self._closed_reason is not None:
            raise BrowserError(f"{method}: connection is closed ({self._closed_reason})")
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = (method, future)
        try:
            await self._websocket.send(json.dumps({"id": message_id, **message}))
            return await asyncio.wait_for(future, self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError:
            raise BrowserError(f"{method}: no reply from the browser within the timeout") from None
        except WebSocketError as exc:
            raise BrowserError(f"{method}: {exc}") from exc
        finally:
            self._pending.pop(message_id, None)

    async def _read_loop(self) -> None:
        try:
            while True:
                data = json.loads(await self._websocket.receive())
                message_id = data.get("id")
                if message_id is not None and message_id in self._pending:
                    method, future = self._pending[message_id]
                    if not future.done():
                        self._resolve(method, future, data)
                elif self._is_event(data):
                    for listener in list(self._listeners):
                        listener(data)
        except asyncio.CancelledError:
            self._closed_reason = "closed by the client"
            raise
        except Exception as exc:  # the socket died or sent garbage: every waiter must be told
            self._closed_reason = str(exc) or type(exc).__name__
        finally:
            for method, future in self._pending.values():
                if not future.done():
                    future.set_exception(BrowserError(f"{method}: connection lost ({self._closed_reason})"))

    def _resolve(self, method: str, future: asyncio.Future, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def _is_event(self, data: dict[str, Any]) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        await self._websocket.close()


class CdpConnection(Connection):
    """Chrome DevTools Protocol. Page-level commands need the `session_id` of an attached target (flat sessions)."""

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        return await self._send(method, message, timeout)

    def _resolve(self, method: str, future: asyncio.Future, data: dict[str, Any]) -> None:
        if "error" in data:
            error = data["error"]
            future.set_exception(ProtocolError(method, error.get("code"), error.get("message", "")))
        else:
            future.set_result(data.get("result", {}))

    def _is_event(self, data: dict[str, Any]) -> bool:
        return "method" in data


class BidiConnection(Connection):
    """WebDriver BiDi. Replies carry `type: success|error`; events carry `type: event`."""

    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        return await self._send(method, {"method": method, "params": params or {}}, timeout)

    def _resolve(self, method: str, future: asyncio.Future, data: dict[str, Any]) -> None:
        if data.get("type") == "error" or "error" in data:
            future.set_exception(ProtocolError(method, data.get("error"), data.get("message", "")))
        else:
            future.set_result(data.get("result", {}))

    def _is_event(self, data: dict[str, Any]) -> bool:
        return data.get("type") == "event"


_SPECIAL_NUMBERS = {"NaN": float("nan"), "-0": -0.0, "Infinity": float("inf"), "-Infinity": float("-inf")}


def deserialize_remote_value(remote: dict[str, Any]) -> Any:
    """BiDi `RemoteValue` to plain Python, mirroring what CDP's `returnByValue` yields.

    Primitives, arrays and objects become Python values; `undefined` and `null` become None (object properties whose
    value is `undefined` are dropped, as JSON does); maps become dicts and sets become lists. Values with no JSON
    equivalent (nodes, functions, windows...) are returned as the raw BiDi dict so no information is lost.
    """
    kind = remote.get("type")
    value = remote.get("value")
    if kind in ("undefined", "null"):
        return None
    if kind in ("string", "boolean"):
        return value
    if kind == "number":
        return _SPECIAL_NUMBERS.get(value, value) if isinstance(value, str) else value
    if kind == "bigint":
        return int(value)
    if kind in ("array", "set", "nodelist", "htmlcollection") and value is not None:
        return [deserialize_remote_value(item) for item in value]
    if kind in ("object", "map") and value is not None:
        result = {}
        for key, item in value:
            if kind == "object" and item.get("type") == "undefined":
                continue  # JSON semantics, as CDP's returnByValue: undefined properties vanish
            key = deserialize_remote_value(key) if isinstance(key, dict) else key
            result[key if isinstance(key, (str, int, float, bool)) or key is None else str(key)] = (
                deserialize_remote_value(item)
            )
        return result
    if kind == "date":
        return value
    if kind == "regexp":
        return f"/{value.get('pattern', '')}/{value.get('flags', '')}"
    return remote
