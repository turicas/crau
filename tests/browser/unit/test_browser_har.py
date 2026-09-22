from __future__ import annotations

import asyncio
import base64

from crau.browser.har import build_har, summarize_har, value_shape
from crau.browser.network import NetworkMonitor


class FakeBackendTab:
    def __init__(self, bodies=None, post_bodies=None, supports_bodies=True):
        self.bodies, self.post_bodies, self.supports_bodies = bodies or {}, post_bodies or {}, supports_bodies
        self.body_requests = []

    async def start_body_capture(self):
        return self.supports_bodies

    async def response_body(self, key):
        self.body_requests.append(key)
        return self.bodies.get(key)

    async def request_body(self, key):
        return self.post_bodies.get(key)


def request(key, url, method="GET", **extra):
    base = {
        "kind": "request",
        "id": key,
        "url": url,
        "method": method,
        "headers": [("Accept", "*/*")],
        "post_data": None,
    }
    base.update(has_post_data=False, time=1_790_000_000.5, resource_type="Fetch", is_navigation=False)
    return {**base, **extra}


def response(key, status=200, mime="application/json", **extra):
    base = {"kind": "response", "id": key, "url": "", "status": status, "status_text": "OK", "headers": []}
    base.update(mime_type=mime, protocol="h2", remote_ip="203.0.113.7", wait_ms=12.5)
    return {**base, **extra}


async def record(events, backend) -> dict:
    monitor = NetworkMonitor(backend)
    await monitor.start_recording()
    for event in events:
        monitor.handle(event)
        await asyncio.sleep(0)
    return build_har(await monitor.stop_recording(), "firefox 156.0", "9.9")


async def test_entry_carries_query_cookies_post_data_bodies_and_timing():
    events = [
        request("1#0", "https://x.org/api?q=a%20b&empty=", "POST", has_post_data=True, is_navigation=False),
        {
            "kind": "request_headers",
            "id": "1#0",
            "headers": [("Cookie", "a=1; b=x=y"), ("Content-Type", "application/json")],
        },
        response("1#0", headers=[("Content-Type", "application/json"), ("Set-Cookie", "sid=9; Path=/; HttpOnly")]),
        {"kind": "finished", "id": "1#0", "encoded_size": 17},
    ]
    backend = FakeBackendTab(bodies={"1#0": b'{"ok": true}'}, post_bodies={"1#0": '{"sent": 1}'})
    har = await record(events, backend)
    assert har["log"]["browser"] == {"name": "firefox", "version": "156.0"}
    (entry,) = har["log"]["entries"]
    assert entry["startedDateTime"] == "2026-09-21T14:13:20.500+00:00" and entry["time"] == 12.5
    assert entry["request"]["queryString"] == [{"name": "q", "value": "a b"}, {"name": "empty", "value": ""}]
    assert entry["request"]["cookies"] == [{"name": "a", "value": "1"}, {"name": "b", "value": "x=y"}]
    assert entry["request"]["postData"] == {"mimeType": "application/json", "text": '{"sent": 1}'}
    assert entry["request"]["bodySize"] == 11 and entry["request"]["httpVersion"] == "HTTP/2"
    assert entry["response"]["cookies"] == [{"name": "sid", "value": "9"}]
    assert entry["response"]["content"] == {"size": 12, "mimeType": "application/json", "text": '{"ok": true}'}
    assert entry["response"]["bodySize"] == 17 and entry["serverIPAddress"] == "203.0.113.7"


async def test_headers_that_arrive_before_their_request_are_not_lost():
    events = [
        {"kind": "request_headers", "id": "7#0", "headers": [("Cookie", "early=1")]},
        {"kind": "response_headers", "id": "7#0", "headers": [("Set-Cookie", "raw=1")]},
        request("7#0", "https://x.org/"),
        response("7#0", mime="text/html", headers=[("Content-Type", "text/html")]),
        {"kind": "finished", "id": "7#0", "encoded_size": 0},
    ]
    (entry,) = (await record(events, FakeBackendTab()))["log"]["entries"]
    assert entry["request"]["headers"] == [{"name": "Cookie", "value": "early=1"}]
    assert entry["response"]["headers"] == [{"name": "Set-Cookie", "value": "raw=1"}]


async def test_redirect_hops_failures_and_binary_bodies():
    events = [
        request("2#0", "https://x.org/old", is_navigation=True),
        response("2#0", status=302, mime="", headers=[("Location", "/new")]),
        {"kind": "finished", "id": "2#0", "encoded_size": 0},
        request("2#1", "https://x.org/new", is_navigation=True),
        response("2#1", mime="image/png"),
        {"kind": "finished", "id": "2#1", "encoded_size": 4},
        request("3#0", "https://blocked.example/"),
        {"kind": "failed", "id": "3#0", "error": "net::ERR_NAME_NOT_RESOLVED"},
    ]
    backend = FakeBackendTab(bodies={"2#1": b"\x89PNG"})
    old, new, failed = (await record(events, backend))["log"]["entries"]
    assert (old["response"]["status"], old["response"]["redirectURL"]) == (302, "/new")
    assert new["response"]["content"]["encoding"] == "base64"
    assert base64.b64decode(new["response"]["content"]["text"]) == b"\x89PNG"
    assert failed["response"]["status"] == 0 and failed["response"]["_error"] == "net::ERR_NAME_NOT_RESOLVED"
    assert "2#0" not in backend.body_requests or old["response"]["content"].get("text") is None


async def test_browser_without_body_support_still_yields_entries():
    events = [request("1#0", "https://x.org/"), response("1#0"), {"kind": "finished", "id": "1#0", "encoded_size": 5}]
    backend = FakeBackendTab(bodies={"1#0": b"never asked"}, supports_bodies=False)
    (entry,) = (await record(events, backend))["log"]["entries"]
    assert "text" not in entry["response"]["content"] and backend.body_requests == []


def test_monitor_tracks_inflight_and_main_document():
    monitor = NetworkMonitor(FakeBackendTab())
    monitor.handle(request("1#0", "https://x.org/start", is_navigation=True, resource_type="Document"))
    monitor.handle(request("2#0", "https://x.org/app.js"))
    assert monitor.inflight == {"1#0", "2#0"}
    monitor.handle(response("1#0", status=404, mime="text/html"))
    monitor.handle({"kind": "finished", "id": "1#0", "encoded_size": 1})
    monitor.handle({"kind": "failed", "id": "2#0", "error": "aborted"})
    assert monitor.inflight == set() and monitor.main_document("https://x.org/start").status == 404
    monitor.reset_navigation()
    assert monitor.records == {} and monitor.main_document("https://x.org/start") is None


def test_value_shape_keeps_structure_and_drops_values():
    data = {"items": [{"id": 1, "tags": ["a"], "meta": None, "ok": True}], "total": 1.5, "empty": []}
    assert value_shape(data) == {
        "items": [{"id": "number", "tags": ["string"], "meta": "null", "ok": "boolean"}],
        "total": "number", "empty": [],
    }  # fmt: skip
    assert value_shape({"a": {"b": {"c": {"d": {"e": 1}}}}}) == {"a": {"b": {"c": {"d": "object"}}}}


def test_summary_describes_calls_without_leaking_values():
    def entry(url, method="GET", mime="application/json", text='{"secret": "v4lue", "n": [1]}', post=None):
        request_part = {"method": method, "url": url, "headers": [{"name": "Authorization", "value": "Bearer t0ken"}]}
        request_part["queryString"] = [{"name": "page", "value": "2"}]
        if post:
            request_part["postData"] = post
        content = {"mimeType": mime, "size": len(text), "text": text}
        return {"request": request_part, "response": {"status": 200, "content": content}}

    form = {"mimeType": "application/x-www-form-urlencoded; charset=UTF-8", "text": "user=bob&pass=hunter2"}
    graphql = {"mimeType": "application/json", "text": '{"query": "q", "v": {"id": 1}}'}
    har = {"log": {"entries": [
        entry("https://x.org/api/items?page=2"),
        entry("https://x.org/login", "POST", post=form),
        entry("https://x.org/graphql", "POST", post=graphql),
        entry("https://x.org/style.css", mime="text/css", text="body{}"),
    ]}}  # fmt: skip
    rows = summarize_har(har)
    assert [row["entry"] for row in rows] == [0, 1, 2, 3]
    assert rows[0]["endpoint"] == "https://x.org/api/items" and rows[0]["query"] == ["page"]
    assert rows[0]["response_body"] == {"secret": "string", "n": ["number"]} and rows[0]["request_headers"] == [
        "Authorization"
    ]
    assert rows[1]["request_body"] == {"user": "string", "pass": "string"}
    assert rows[2]["request_body"] == {"query": "string", "v": {"id": "number"}}
    assert rows[3]["response_body"] is None
    flat = repr(rows)
    assert "v4lue" not in flat and "hunter2" not in flat and "t0ken" not in flat
    assert [row["entry"] for row in summarize_har(har, url_filter=r"/api/|graphql")] == [0, 2]
    assert [row["entry"] for row in summarize_har(har, method="post")] == [1, 2]
    assert [row["entry"] for row in summarize_har(har, mime_filter="css")] == [3]
