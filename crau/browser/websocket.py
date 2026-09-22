"""Minimal RFC 6455 WebSocket client on top of asyncio streams (standard library only).

Scope: `ws://` to a local browser, text messages carrying JSON. No TLS, no extensions (none is offered, so the server
cannot enable compression) and no `Origin` header, because Firefox and Lightpanda reject connections that carry one.
Parsing is incremental: bytes are buffered until a whole frame is available, so a slow or split delivery can never
desynchronize the stream.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(ConnectionError):
    """Handshake or framing problem."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (close frame or EOF)."""


@dataclass
class Frame:
    fin: bool
    opcode: int
    payload: bytes


def accept_key(key: str) -> str:
    """Value the server must send in `Sec-WebSocket-Accept` for a given `Sec-WebSocket-Key`."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def parse_headers(lines: list[str]) -> dict[str, str]:
    """HTTP header lines to a dict with lowercase names."""
    pairs = (line.partition(":") for line in lines if ":" in line)
    return {name.strip().lower(): value.strip() for name, _, value in pairs}


def apply_mask(data: bytes, key: bytes) -> bytes:
    """XOR `data` with the repeating 4-byte `key` (big-integer XOR: fast enough for multi-megabyte payloads)."""
    size = len(data)
    if size == 0:
        return b""
    repeated = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(size, "big")


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: bool = True, fin: bool = True) -> bytes:
    """Serialize one frame. Clients must mask (`mask=True`); servers must not."""
    first = (0x80 if fin else 0x00) | opcode
    mask_bit = 0x80 if mask else 0x00
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", first, mask_bit | size)
    elif size < 65536:
        header = struct.pack("!BBH", first, mask_bit | 126, size)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, size)
    if not mask:
        return header + payload
    key = os.urandom(4)
    return header + key + apply_mask(payload, key)


class FrameParser:
    """Incremental frame decoder: `feed()` bytes as they arrive and get back only the frames that are complete."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer += data
        frames = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _next_frame(self) -> Frame | None:
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        masked = bool(second & 0x80)
        size = second & 0x7F
        offset = 2
        if size == 126:
            if len(buffer) < 4:
                return None
            size = struct.unpack_from("!H", buffer, 2)[0]
            offset = 4
        elif size == 127:
            if len(buffer) < 10:
                return None
            size = struct.unpack_from("!Q", buffer, 2)[0]
            offset = 10
        key_size = 4 if masked else 0
        if len(buffer) < offset + key_size + size:
            return None
        key = bytes(buffer[offset : offset + key_size])
        start = offset + key_size
        payload = bytes(buffer[start : start + size])
        del buffer[: start + size]
        if masked:
            payload = apply_mask(payload, key)
        return Frame(fin=bool(first & 0x80), opcode=first & 0x0F, payload=payload)


class WebSocket:
    """Connected client. Use `await WebSocket.connect(url)`, then `send()` / `receive()` text messages."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._parser = FrameParser()
        self._messages: list[str] = []
        self._fragments: list[bytes] = []
        self._fragment_opcode = OP_TEXT
        self.closed = False

    @classmethod
    async def connect(cls, url: str, timeout: float = 10.0) -> WebSocket:
        parts = urlsplit(url)
        if parts.scheme != "ws" or not parts.hostname:
            raise WebSocketError(f"Only ws:// URLs are supported, got {url!r}")
        port = parts.port or 80
        reader, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, port), timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        try:
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as exc:
            writer.close()
            raise WebSocketError(f"WebSocket handshake with {url} failed: {exc!r}") from exc
        lines = head.decode("latin-1").split("\r\n")
        headers = parse_headers(lines[1:])
        if " 101 " not in lines[0] + " " or headers.get("sec-websocket-accept") != accept_key(key):
            writer.close()
            raise WebSocketError(f"WebSocket handshake with {url} refused: {lines[0]!r}")
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        if self.closed:
            raise WebSocketClosed("WebSocket is closed")
        self._writer.write(encode_frame(text.encode("utf-8")))
        await self._writer.drain()

    async def receive(self) -> str:
        """Next complete text message. Answers pings, reassembles fragments, raises `WebSocketClosed` at the end."""
        while not self._messages:
            if self.closed:
                raise WebSocketClosed("WebSocket is closed")
            data = await self._reader.read(1 << 16)
            if not data:
                self.closed = True
                raise WebSocketClosed("Connection closed by the browser")
            for frame in self._parser.feed(data):
                await self._handle(frame)
        return self._messages.pop(0)

    async def _handle(self, frame: Frame) -> None:
        if frame.opcode == OP_PING:
            self._writer.write(encode_frame(frame.payload, OP_PONG))
            await self._writer.drain()
        elif frame.opcode == OP_CLOSE:
            self.closed = True
        elif frame.opcode in (OP_TEXT, OP_BINARY, OP_CONTINUATION):
            if frame.opcode != OP_CONTINUATION:
                self._fragments = []
                self._fragment_opcode = frame.opcode
            self._fragments.append(frame.payload)
            if frame.fin:
                payload = b"".join(self._fragments)
                self._fragments = []
                if self._fragment_opcode == OP_TEXT:
                    self._messages.append(payload.decode("utf-8"))

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._writer.write(encode_frame(struct.pack("!H", 1000), OP_CLOSE))
                await self._writer.drain()
            except (OSError, RuntimeError):
                pass
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        except (OSError, asyncio.TimeoutError):
            pass
