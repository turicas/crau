from __future__ import annotations

import asyncio
import math

import pytest
from crau.browser.connection import BidiConnection, CdpConnection, deserialize_remote_value
from crau.browser.errors import BrowserError, ProtocolError
from fakes import FakeWebSocketServer, ScriptedBrowser


async def test_cdp_call_returns_result_and_forwards_session_id():
    fake = ScriptedBrowser("cdp", {"Runtime.evaluate": {"result": {"value": 2}}})
    async with FakeWebSocketServer(fake.handler) as server:
        connection = await CdpConnection.connect(server.url)
        result = await connection.call("Runtime.evaluate", {"expression": "1+1"}, session_id="S1")
        await connection.close()
    assert result == {"result": {"value": 2}}
    assert fake.received[0]["sessionId"] == "S1" and fake.received[0]["params"] == {"expression": "1+1"}


async def test_cdp_error_keeps_code_and_message():
    fake = ScriptedBrowser("cdp", {"Page.navigate": {"__error__": {"code": -32000, "message": "Cannot navigate"}}})
    async with FakeWebSocketServer(fake.handler) as server:
        connection = await CdpConnection.connect(server.url)
        with pytest.raises(ProtocolError) as info:
            await connection.call("Page.navigate", {"url": "x"})
        await connection.close()
    assert info.value.code == -32000 and "Cannot navigate" in str(info.value) and "Page.navigate" in str(info.value)


async def test_bidi_error_keeps_the_full_message():
    error = {"error": "unsupported operation", "message": "System access is required", "stacktrace": ""}
    fake = ScriptedBrowser("bidi", {"script.evaluate": {"__error__": error}})
    async with FakeWebSocketServer(fake.handler) as server:
        connection = await BidiConnection.connect(server.url)
        with pytest.raises(ProtocolError) as info:
            await connection.call("script.evaluate", {})
        await connection.close()
    assert info.value.code == "unsupported operation" and "System access is required" in str(info.value)


async def test_events_arriving_while_a_command_is_pending_reach_listeners():
    async def slow_navigate(message, connection):
        await connection.send_json({"method": "Network.requestWillBeSent", "params": {"requestId": "1"}})
        await connection.send_json({"method": "Page.loadEventFired", "params": {}, "sessionId": "S"})
        return {"frameId": "F"}

    fake = ScriptedBrowser("cdp", {"Page.navigate": slow_navigate})
    async with FakeWebSocketServer(fake.handler) as server:
        connection = await CdpConnection.connect(server.url)
        events = []
        connection.add_listener(events.append)
        waiter = asyncio.ensure_future(connection.wait_for_event(lambda e: e["method"] == "Page.loadEventFired", 2))
        await asyncio.sleep(0)
        assert await connection.call("Page.navigate", {"url": "x"}) == {"frameId": "F"}
        assert (await waiter)["sessionId"] == "S"
        await connection.close()
    assert [event["method"] for event in events] == ["Network.requestWillBeSent", "Page.loadEventFired"]


async def test_concurrent_calls_are_matched_by_id_even_out_of_order():
    async def delayed(message, connection):
        await asyncio.sleep(0.05 if message["params"]["n"] == 1 else 0)
        return {"n": message["params"]["n"]}

    async def handler(connection):
        tasks = []
        while (message := await connection.receive_json()) is not None:

            async def answer(message=message):
                await connection.send_json({"id": message["id"], "result": await delayed(message, connection)})

            tasks.append(asyncio.ensure_future(answer()))

    async with FakeWebSocketServer(handler) as server:
        connection = await CdpConnection.connect(server.url)
        results = await asyncio.gather(connection.call("X.y", {"n": 1}), connection.call("X.y", {"n": 2}))
        await connection.close()
    assert results == [{"n": 1}, {"n": 2}]


async def test_pending_call_fails_when_connection_drops_and_later_calls_fail_fast():
    async def handler(connection):
        await connection.receive_json()

    async with FakeWebSocketServer(handler) as server:
        connection = await CdpConnection.connect(server.url)
        with pytest.raises(BrowserError, match="connection lost"):
            await connection.call("Browser.getVersion")
        with pytest.raises(BrowserError, match="closed"):
            await connection.call("Browser.getVersion")
        await connection.close()


async def test_unanswered_call_times_out_with_method_name():
    fake = ScriptedBrowser("cdp", {"Slow.method": None})
    async with FakeWebSocketServer(fake.handler) as server:
        connection = await CdpConnection.connect(server.url)
        with pytest.raises(BrowserError, match="Slow.method"):
            await connection.call("Slow.method", timeout=0.1)
        await connection.close()


def number(value):
    return {"type": "number", "value": value}


def test_remote_value_primitives():
    assert deserialize_remote_value({"type": "undefined"}) is None
    assert deserialize_remote_value({"type": "null"}) is None
    assert deserialize_remote_value({"type": "string", "value": "á"}) == "á"
    assert deserialize_remote_value({"type": "boolean", "value": False}) is False
    assert deserialize_remote_value({"type": "number", "value": 1.5}) == 1.5
    assert deserialize_remote_value({"type": "number", "value": "Infinity"}) == math.inf
    assert math.isnan(deserialize_remote_value({"type": "number", "value": "NaN"}))
    assert deserialize_remote_value({"type": "bigint", "value": "12345678901234567890"}) == 12345678901234567890


def test_remote_value_nested_object_as_firefox_sends_it():
    remote = {
        "type": "object",
        "value": [
            ["a", number(1)],
            ["b", {"type": "array", "value": [number(1), {"type": "string", "value": "x"}, {"type": "null"}]}],
            ["when", {"type": "date", "value": "2026-09-21T00:00:00.000Z"}],
            ["gone", {"type": "undefined"}],
        ],
    }
    assert deserialize_remote_value(remote) == {"a": 1, "b": [1, "x", None], "when": "2026-09-21T00:00:00.000Z"}


def test_remote_value_map_set_regexp_and_unserializable():
    remote_map = {"type": "map", "value": [[number(1), {"type": "string", "value": "one"}]]}
    assert deserialize_remote_value(remote_map) == {1: "one"}
    assert deserialize_remote_value({"type": "set", "value": [number(1)]}) == [1]
    assert deserialize_remote_value({"type": "regexp", "value": {"pattern": "a+", "flags": "g"}}) == "/a+/g"
    node = {"type": "node", "sharedId": "abc", "value": {"nodeType": 1}}
    assert deserialize_remote_value(node) == node
