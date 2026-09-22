"""Local website used by the integration tests, the examples and the smoke test (standard library only).

Run it standalone with `python tests/browser/testsite.py [PORT]` to poke at the pages by hand.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PARAGRAPHS = "\n".join(f'<p class="filler">Paragraph {number}</p>' for number in range(120))

INDEX = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Test site</title></head>
<body>
<h1 id="main-title">Test site</h1>
<nav><a href="/final">Final page</a> <a id="to-form" href="/form">Form page</a></nav>
<iframe title="inner" srcdoc="<button>inside iframe</button>"></iframe>
{PARAGRAPHS}
<p id="bottom">The bottom</p>
<script>
  console.log("index loaded", 42);
  setTimeout(() => {{
    const late = document.createElement("div");
    late.id = "late"; late.textContent = "hydrated";
    document.body.appendChild(late);
  }}, 400);
  fetch("/api/echo", {{method: "POST", headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
                      body: "field=value&other=1"}});
</script>
</body></html>"""

FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Form</title></head>
<body>
<h1>Sign in</h1>
<form id="login" onsubmit="return false">
  <label for="user">User name</label> <input id="user" name="user">
  <label>Password <input type="password" name="password"></label>
  <input name="search" placeholder="Search term">
  <label for="color">Colour</label>
  <select id="color" name="color"><option value="r">Red</option><option value="g">Green</option></select>
  <label><input type="checkbox" name="remember"> Remember me</label>
  <button type="button" id="submit">Sign in</button>
</form>
<p id="mirror"></p>
<div id="result"></div>
<div style="height: 2500px"></div>
<button type="button" id="far">Far away</button>
<script>
  // plain DOM only (no form.fieldName shortcuts): Lightpanda does not implement named form access
  const field = (name) => document.querySelector("[name=" + name + "]");
  // value tracking the way frameworks do it: only real input events are noticed
  field("user").addEventListener("input", (event) => {
    document.getElementById("mirror").textContent = event.target.value;
  });
  field("search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") document.title = "searched:" + field("search").value;
  });
  document.getElementById("submit").addEventListener("click", async (event) => {
    const payload = {user: field("user").value, password: field("password").value, color: field("color").value,
                     remember: field("remember").checked, trusted: event.isTrusted};
    const options = {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)};
    const reply = await fetch("/api/echo", options);
    document.getElementById("result").textContent = JSON.stringify((await reply.json()).received);
    document.title = "signed-in";
  });
  document.getElementById("far").addEventListener("click", () => { document.title = "far-clicked"; });
</script>
</body></html>"""

SLOW = """<!doctype html><html><head><meta charset="utf-8"><title>Slow</title></head><body>
<div id="out">waiting</div>
<script>
  setTimeout(async () => {
    const reply = await fetch("/api/slow");
    document.getElementById("out").textContent = (await reply.json()).value;
  }, 300);
</script></body></html>"""

DIALOG = """<!doctype html><html><head><meta charset="utf-8"><title>before</title></head><body>
<script>alert("hello from alert"); document.title = "after-alert";</script></body></html>"""

DDG = """<!doctype html><html><head><meta charset="utf-8"><title>q at DuckDuckGo</title></head><body>
<div class="results">
<div class="result results_links results_links_deep web-result"><div class="links_main links_deep result__body">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone%3Fa%3D1&amp;rut=abc">First <b>result</b></a></h2>
  <a class="result__snippet" href="#">Snippet of the <b>first</b> result.</a></div></div>
<div class="result results_links results_links_deep web-result"><div class="links_main links_deep result__body">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://example.com/two">Second result</a></h2>
  <a class="result__snippet" href="#">Second snippet.</a></div></div>
<div class="result result--ad"><h2 class="result__title"><a class="result__a"
   href="//duckduckgo.com/y.js?ad_provider=x">Sponsored</a></h2></div>
</div></body></html>"""

BING = """<!doctype html><html><head><meta charset="utf-8"><title>q - Bing</title></head><body><ol id="b_results">
<li class="b_algo"><h2><a
  href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9vbmU_YT0x&amp;ntb=1">First
  result</a></h2>
  <div class="b_caption"><p>Snippet of the first result.</p></div></li>
<li class="b_algo"><h2><a href="https://example.com/two">Second result</a></h2>
  <div class="b_caption"><p>Second snippet.</p></div></li>
<li class="b_ad"><h2><a href="https://ads.example/">Sponsored</a></h2></li>
</ol></body></html>"""

PAGES = {
    "/": INDEX,
    "/form": FORM,
    "/slow": SLOW,
    "/dialog": DIALOG,
    "/final": "<!doctype html><html><head><title>Final</title></head><body><h1>Final page</h1></body></html>",
    "/ddg": DDG,
    "/bing": BING,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200, headers=None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json", headers)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        self.server.requests.append(("GET", self.path, dict(self.headers.items())))
        if path in PAGES:
            self._send(200, PAGES[path].encode("utf-8"))
        elif path == "/echo":
            self._json({name.lower(): value for name, value in self.headers.items()})
        elif path == "/json":
            self._json({"items": [{"id": 1, "name": "one"}], "total": 1})
        elif path == "/api/slow":
            time.sleep(0.7)
            self._json({"value": "slow-done"})
        elif path == "/redirect":
            self._send(302, b"", headers=[("Location", "/final")])
        elif path == "/set-cookie":
            persistent = "; Max-Age=3600" if "persist" in self.path else ""
            self._send(200, b"<p>cookie set</p>", headers=[("Set-Cookie", f"session=abc123; Path=/{persistent}")])
        elif path == "/binary":
            self._send(200, bytes(range(256)), "application/octet-stream")
        else:
            self._send(404, b"<!doctype html><title>Missing</title><h1>Not found</h1>")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
        self.server.requests.append(("POST", self.path, body))
        try:
            received = json.loads(body)
        except ValueError:
            received = body
        self._json({"received": received})

    def log_message(self, *args) -> None:
        pass


class Site:
    """`with Site() as site:` serves on a free port; `site.url` is the base URL, `site.requests` what it received."""

    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.requests = []
        self.requests = self.server.requests
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Site:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    with Site(int(sys.argv[1]) if len(sys.argv) > 1 else 8765) as running:
        print(running.url, file=sys.stderr)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
