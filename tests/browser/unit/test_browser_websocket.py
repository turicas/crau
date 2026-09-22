from __future__ import annotations

import asyncio

import pytest
from crau.browser.websocket import (
    OP_TEXT,
    FrameParser,
    WebSocket,
    WebSocketClosed,
    WebSocketError,
    accept_key,
    apply_mask,
    encode_frame,
)
from fakes import FakeWebSocketServer


def test_accept_key_matches_rfc_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_mask_is_its_own_inverse_for_any_length():
    key = b"\x01\x02\x03\x04"
    for size in (0, 1, 3, 4, 5, 1000):
        data = bytes(range(256)) * 4
        assert apply_mask(apply_mask(data[:size], key), key) == data[:size]


@pytest.mark.parametrize("size", [0, 125, 126, 65535, 65536, 300_000])
@pytest.mark.parametrize("mask", [True, False])
def test_frame_round_trip_for_every_length_encoding(size, mask):
    payload = b"x" * size
    frames = FrameParser().feed(encode_frame(payload, mask=mask))
    assert [(frame.fin, frame.opcode, frame.payload) for frame in frames] == [(True, OP_TEXT, payload)]


def test_parser_waits_for_complete_frames_when_fed_byte_by_byte():
    data = encode_frame(b"first", mask=False) + encode_frame(b"y" * 70000, mask=False)
    parser = FrameParser()
    payloads = []
    for index in range(len(data)):
        payloads.extend(frame.payload for frame in parser.feed(data[index : index + 1]))
    assert payloads == [b"first", b"y" * 70000]


async def test_client_sends_masked_text_without_origin_and_reads_reply():
    async def handler(connection):
        await connection.send("echo:" + await connection.receive())

    async with FakeWebSocketServer(handler) as server:
        websocket = await WebSocket.connect(server.url + "/devtools/browser/abc?x=1")
        await websocket.send("olá")
        assert await websocket.receive() == "echo:olá"
        await websocket.close()
    assert "origin" not in server.request_headers[0]
    assert server.request_headers[0]["host"] == f"127.0.0.1:{server.port}"


async def test_message_split_in_tiny_tcp_chunks_and_in_fragments_is_reassembled():
    big = "á" * 50_000

    async def handler(connection):
        await connection.send(big, chunk_size=7000)
        await connection.send("fragmented message", fragments=4)
        await connection.receive()

    async with FakeWebSocketServer(handler) as server:
        websocket = await WebSocket.connect(server.url)
        assert await websocket.receive() == big
        assert await websocket.receive() == "fragmented message"
        await websocket.close()


async def test_ping_is_answered_with_same_payload():
    pongs = []

    async def handler(connection):
        await connection.ping(b"are-you-there")
        await connection.send("after ping")
        await connection.receive()
        pongs.extend(connection.pongs)

    async with FakeWebSocketServer(handler) as server:
        websocket = await WebSocket.connect(server.url)
        assert await websocket.receive() == "after ping"
        await websocket.close()
        await asyncio.sleep(0.05)
    assert pongs == [b"are-you-there"]


async def test_receive_raises_when_server_goes_away():
    async def handler(connection):
        return

    async with FakeWebSocketServer(handler) as server:
        websocket = await WebSocket.connect(server.url)
        with pytest.raises(WebSocketClosed):
            await websocket.receive()
        with pytest.raises(WebSocketClosed):
            await websocket.send("too late")


async def test_handshake_with_wrong_accept_key_is_refused():
    async def handler(connection):
        await connection.receive()

    async with FakeWebSocketServer(handler, bad_accept=True) as server:
        with pytest.raises(WebSocketError):
            await WebSocket.connect(server.url)


async def test_non_ws_url_is_rejected():
    with pytest.raises(WebSocketError):
        await WebSocket.connect("wss://127.0.0.1:1/")
