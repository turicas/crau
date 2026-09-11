import asyncio
import json
import pytest
import pytest_asyncio
import websockets
from crau.fetchers.cdp import CdpBrowserFetcher


@pytest_asyncio.fixture
async def run_cdp_mock_server():
    servers = []

    async def _start():
        async def handler(websocket):
            async for message in websocket:
                data = json.loads(message)
                msg_id = data.get("id")
                method = data.get("method")

                if method == "Network.enable":
                    await websocket.send(json.dumps({"id": msg_id, "result": {}}))
                elif method == "Page.enable":
                    await websocket.send(json.dumps({"id": msg_id, "result": {}}))
                elif method == "Runtime.enable":
                    await websocket.send(json.dumps({"id": msg_id, "result": {}}))
                elif method == "Page.navigate":
                    await websocket.send(json.dumps({"id": msg_id, "result": {"frameId": "123"}}))
                    # Emit network events for main page
                    await websocket.send(
                        json.dumps({
                            "method": "Network.requestWillBeSent",
                            "params": {
                                "requestId": "req-1",
                                "request": {
                                    "url": data["params"]["url"],
                                    "method": "GET",
                                    "headers": {"User-Agent": "cdp-agent"},
                                },
                                "timestamp": 1000.0,
                            },
                        })
                    )
                    await websocket.send(
                        json.dumps({
                            "method": "Network.responseReceived",
                            "params": {
                                "requestId": "req-1",
                                "response": {
                                    "status": 200,
                                    "statusText": "OK",
                                    "headers": {"Content-Type": "text/html"},
                                    "protocol": "http/1.1",
                                },
                            },
                        })
                    )
                    await websocket.send(
                        json.dumps({
                            "method": "Network.loadingFinished",
                            "params": {"requestId": "req-1", "timestamp": 1000.5},
                        })
                    )
                    # Emit page load event
                    await websocket.send(
                        json.dumps({"method": "Page.loadEventFired", "params": {"timestamp": 1001.0}})
                    )
                elif method == "Network.getResponseBody":
                    req_id = data["params"]["requestId"]
                    if req_id == "req-1":
                        await websocket.send(
                            json.dumps({
                                "id": msg_id,
                                "result": {
                                    "body": "<html><body>Rendered by CDP</body></html>",
                                    "base64Encoded": False,
                                },
                            })
                        )
                elif method == "Runtime.evaluate":
                    await websocket.send(
                        json.dumps({
                            "id": msg_id,
                            "result": {
                                "result": {
                                    "type": "string",
                                    "value": "<html><body>Rendered by CDP</body></html>",
                                }
                            },
                        })
                    )

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        servers.append(server)
        return f"ws://127.0.0.1:{port}"

    yield _start

    for s in servers:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_cdp_browser_fetcher(run_cdp_mock_server):
    ws_endpoint = await run_cdp_mock_server()
    async with CdpBrowserFetcher(endpoint_url=ws_endpoint) as fetcher:
        result = await fetcher.fetch("https://example.com/spa")

    assert len(result.transactions) == 1
    tx = result.transactions[0]
    assert tx.request.url == "https://example.com/spa"
    assert tx.response.status_code == 200
    assert tx.response.raw_body == b"<html><body>Rendered by CDP</body></html>"
    assert "Rendered by CDP" in result.page.content
    assert result.page.url == "https://example.com/spa"
