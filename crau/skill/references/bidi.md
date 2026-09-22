# Raw BiDi recipes (Firefox)

`await tab.bidi(method, params)` sends a command as is: add `"context": tab.id` yourself where the command needs one.
Spec: https://w3c.github.io/webdriver-bidi/ . Every recipe here runs in `tests/browser/integration/test_browser_reference_recipes.py`.

## Find elements by role and name

```python
found = await tab.bidi(
    "browsingContext.locateNodes",
    {"context": tab.id, "locator": {"type": "accessibility", "value": {"role": "link", "name": "Form page"}}},
)
node = found["nodes"][0]  # node["sharedId"], node["value"]["attributes"]
```

Other locator types: `css`, `xpath`, `innerText`.

## Evaluate inside an iframe

```python
tree = await tab.bidi("browsingContext.getTree", {"root": tab.id})
child = tree["contexts"][0]["children"][0]["context"]
inside = await tab.bidi(
    "script.evaluate", {"expression": "document.title", "target": {"context": child}, "awaitPromise": False}
)
value = inside["result"]  # a RemoteValue: {"type": "string", "value": "..."}
```

`crau.browser.connection.deserialize_remote_value()` turns RemoteValues into plain Python.

## Block (or rewrite) requests

```python
await tab.bidi("session.subscribe", {"events": ["network.beforeRequestSent"]})
await tab.bidi(
    "network.addIntercept",
    {"phases": ["beforeRequestSent"], "urlPatterns": [{"type": "pattern", "pathname": "/api/echo"}]},
)
# for every event with params.isBlocked, answer with one of:
#   network.failRequest {request}    network.continueRequest {request, headers?, url?, method?, body?}
#   network.provideResponse {request, statusCode, headers, body}
```

Blocked requests stay pending until answered, so register the listener before navigating (see the test for a
complete listener).

## Run code before any page script

```python
await tab.bidi("script.addPreloadScript", {"functionDeclaration": "() => { window.injectedEarly = 42; }"})
```

Applies to every navigation of every tab for the rest of the session.

## Facts that save time

- Firefox removed CDP in version 141: BiDi is the only remote protocol. Start it with `--remote-debugging-port=0`; the
  real port is in the log line `WebDriver BiDi listening on ws://...` and in `<profile>/WebDriverBiDiServer.json`.
  Connect to `/session` and send `session.new`. No geckodriver, no special preferences.
- The listener only accepts loopback connections that carry no `Origin` header.
- One session at a time, and a dropped socket does not end it: always send `session.end`.
- The first tab of a fresh profile is `about:home`, a privileged page where `script.evaluate` fails with "System
  access is required". Create your own tab with `browsingContext.create`.
- `application/json` documents are replaced by the JSON viewer's DOM; the library turns it off with the preference
  `devtools.jsonview.enabled=false`.
- `navigator.webdriver` is `true` natively. The stealth preload defines the getter on `Navigator.prototype`, which
  leaves no own property on `navigator`.
- Scrolling settles on the next frame: measure an element one frame after `scrollIntoView` or clicks miss sometimes.
- `browsingContext.captureScreenshot` takes `origin: "document"` for the full page and `clip` for a box or an element;
  `browsingContext.print` returns a PDF; `browsingContext.setViewport` resizes. All present in ESR 140 too.
- Not in ESR 140 (`unknown command`): `network.addDataCollector`/`network.getData` and `network.setExtraHeaders`.
  A data collector bigger than 200,000,000 bytes is refused with `invalid argument`.
