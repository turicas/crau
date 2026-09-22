"""In-process fakes used by the offline tests: a WebSocket server and scripted CDP / BiDi browsers on top of it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from crau.browser.websocket import OP_CLOSE, OP_PING, OP_TEXT, FrameParser, accept_key, encode_frame, parse_headers


class ServerConnection:
    """Server side of one WebSocket connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, headers: dict) -> None:
        self.reader, self.writer, self.path, self.headers = reader, writer, path, headers
        self._parser = FrameParser()
        self._frames: list = []
        self.pongs: list[bytes] = []

    async def send(self, text: str, chunk_size: int | None = None, fragments: int = 1) -> None:
        payload = text.encode("utf-8")
        if fragments > 1:
            step = max(1, len(payload) // fragments)
            pieces = [payload[start : start + step] for start in range(0, len(payload), step)]
            data = b"".join(
                encode_frame(piece, OP_TEXT if index == 0 else 0x0, mask=False, fin=index == len(pieces) - 1)
                for index, piece in enumerate(pieces)
            )
        else:
            data = encode_frame(payload, mask=False)
        for start in range(0, len(data), chunk_size or len(data)):
            self.writer.write(data[start : start + (chunk_size or len(data))])
            await self.writer.drain()
            if chunk_size:
                await asyncio.sleep(0.001)

    async def send_json(self, message: dict) -> None:
        await self.send(json.dumps(message))

    async def ping(self, payload: bytes) -> None:
        self.writer.write(encode_frame(payload, OP_PING, mask=False))
        await self.writer.drain()

    async def receive(self) -> str | None:
        """Next text message from the client, or None when the client closed."""
        while True:
            while self._frames:
                frame = self._frames.pop(0)
                if frame.opcode == OP_TEXT:
                    return frame.payload.decode("utf-8")
                if frame.opcode == OP_CLOSE:
                    return None
                if frame.opcode == 0xA:
                    self.pongs.append(frame.payload)
            data = await self.reader.read(1 << 16)
            if not data:
                return None
            self._frames.extend(self._parser.feed(data))

    async def receive_json(self) -> dict | None:
        text = await self.receive()
        return None if text is None else json.loads(text)


class FakeWebSocketServer:
    """`async with FakeWebSocketServer(handler) as server:` then connect to `server.url`."""

    def __init__(self, handler: Callable[[ServerConnection], Awaitable[None]], bad_accept: bool = False) -> None:
        self.handler = handler
        self.bad_accept = bad_accept
        self.request_headers: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def __aenter__(self) -> FakeWebSocketServer:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._tasks.add(asyncio.current_task())
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            path = head[0].split(" ")[1]
            headers = parse_headers(head[1:])
            self.request_headers.append(headers)
            accept = "wrong" if self.bad_accept else accept_key(headers["sec-websocket-key"])
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            await self.handler(ServerConnection(reader, writer, path, headers))
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


class ScriptedBrowser:
    """Fake protocol endpoint: `replies` maps a method name to a result dict, an `Exception`-like error dict
    (`{"__error__": {...}}`) or a callable `(message, connection) -> result`. Every received message is recorded."""

    def __init__(self, protocol: str, replies: dict | None = None) -> None:
        self.protocol = protocol
        self.replies = replies or {}
        self.received: list[dict] = []
        self.connections: list[ServerConnection] = []

    async def handler(self, connection: ServerConnection) -> None:
        self.connections.append(connection)
        while True:
            message = await connection.receive_json()
            if message is None:
                return
            self.received.append(message)
            reply = self.replies.get(message["method"], {})
            if callable(reply):
                reply = reply(message, connection)
                if asyncio.iscoroutine(reply):
                    reply = await reply
            if reply is None:
                continue  # scripted silence: the command never gets an answer
            await connection.send_json(self._envelope(message, reply))

    def _envelope(self, message: dict, reply: dict) -> dict:
        error = reply.get("__error__") if isinstance(reply, dict) else None
        if self.protocol == "cdp":
            envelope = {"id": message["id"], "error": error} if error else {"id": message["id"], "result": reply}
            if "sessionId" in message:
                envelope["sessionId"] = message["sessionId"]
            return envelope
        if error:
            return {"type": "error", "id": message["id"], **error}
        return {"type": "success", "id": message["id"], "result": reply}
