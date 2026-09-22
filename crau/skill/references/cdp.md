# Raw CDP recipes (Chromium, Lightpanda)

`await tab.cdp(method, params)` sends a command on the tab's session. Protocol reference:
https://chromedevtools.github.io/devtools-protocol/ . Every recipe here runs in
`tests/browser/integration/test_browser_reference_recipes.py`.

## Accessibility tree

```python
nodes = (await tab.cdp("Accessibility.getFullAXTree"))["nodes"]
named = [(n["role"]["value"], n.get("name", {}).get("value")) for n in nodes if not n.get("ignored")]
```

`tab.outline()` is usually enough and works on every browser; the AX tree adds states and relationships. Lightpanda
rejects this call without parameters.

## Evaluate inside an iframe

```python
tree = (await tab.cdp("Page.getFrameTree"))["frameTree"]
frame_id = tree["childFrames"][0]["frame"]["id"]
world = await tab.cdp("Page.createIsolatedWorld", {"frameId": frame_id})
result = await tab.cdp(
    "Runtime.evaluate",
    {"expression": "document.title", "contextId": world["executionContextId"], "returnByValue": True},
)
```

Clicking inside iframes needs nothing special: `tab.click()` uses page coordinates, which pass through frames, but the
target lookup only sees the top document, so compute the coordinates yourself or click via the isolated world.

## Block requests

```python
await tab.cdp("Network.setBlockedURLs", {"urls": ["*/analytics/*", "*.woff2"]})  # before goto()
```

## Downloads

```python
await tab.cdp("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/abs/dir"})
```

Then trigger the download (click the link) and poll the directory.

## No TCP port: `--remote-debugging-pipe`

Chromium can speak CDP over two inherited file descriptors instead of a WebSocket: it reads commands on fd 3 and
writes replies on fd 4, each message terminated by a NUL byte. Useful where opening ports is forbidden. Only the
process that launched the browser can talk to it, so it cannot back named instances, which is why the library uses
port 0 instead. The catch when doing it by hand from Python: pipe descriptors are close-on-exec and `dup2(n, n)` keeps
that flag, so duplicate to high numbers first:

```python
to_r, to_w = os.pipe()
from_r, from_w = os.pipe()


def wire():
    hi_r, hi_w = os.dup(to_r), os.dup(from_w)
    os.dup2(hi_r, 3, inheritable=True)
    os.dup2(hi_w, 4, inheritable=True)


proc = subprocess.Popen(
    [
        chromium,
        "--headless=new",
        "--no-sandbox",
        "--remote-debugging-pipe",
        f"--user-data-dir={profile}",
        "about:blank",
    ],
    preexec_fn=wire,
    close_fds=False,
)
os.write(to_w, json.dumps({"id": 1, "method": "Browser.getVersion"}).encode() + b"\0")
# read from from_r until b"\0"
```

## Facts that save time

- Since Chrome 136, `--remote-debugging-port` and `--remote-debugging-pipe` are ignored on the default profile: a
  `--user-data-dir` is mandatory, and without it Chrome starts normally and never opens the endpoint.
- `--headless=old` no longer selects anything on the `chrome` binary; the old mode lives in `chrome-headless-shell`.
- `--disable-blink-features=AutomationControlled` is what makes `navigator.webdriver` false under remote debugging.
- `--user-agent=` keeps the header, the JS value and `sec-ch-ua` coherent; `Emulation.setUserAgentOverride` without
  `userAgentMetadata` empties `navigator.userAgentData.brands` and drops the `sec-ch-ua` header.
- A target that was just closed can still be listed by `Target.getTargets` for a moment; attaching to it hangs.
- To attach to a browser a person is using, Chrome 144+ has `chrome://inspect/#remote-debugging`; the endpoint is in
  `<profile>/DevToolsActivePort` (port on line 1, path on line 2) and there is no `/json/version`. Not tested here.
