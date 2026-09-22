# Network capture (HAR)

```bash
crau har capture https://example.com/app out.har -I      # -I: wait for the network to go quiet
crau har summary out.har -M json                          # what is in there, without the values
```

The HAR is complete and unfiltered: request and response headers as really sent (cookies and authorization included),
query strings, POST bodies, response bodies (text as text, binary as base64), every redirect hop as its own entry,
failed requests with `_error`. Treat the file as a secret.

`har summary` prints one block per entry: method, status, endpoint, query parameter names, request header names and
the *shape* of request and response bodies (`{"items": [{"id": "number"}]}`), so an agent can find the interesting
endpoints for a few hundred tokens instead of reading megabytes. Filters: `-u REGEX` URL, `-m METHOD`, `-M TEXT`
response MIME type, `-J` JSON output.

## Recording around interaction

Requests that matter often happen after a click. Use the library:

```python
async with Browser("chromium") as browser:
    tab = await browser.new_tab()
    bodies = await tab.start_recording()  # False: this browser cannot provide bodies (entries still recorded)
    await tab.goto("https://example.com/search")
    await tab.fill("label=Query", "open data")
    await tab.click("text=Search")
    await tab.wait(selector=".results", idle=True)
    har = await tab.har()  # stops recording; dict ready for json.dump
```

`examples/browser_capture_har.py` is a complete version.

## What each browser provides

| | Chromium | Firefox (current) | Firefox ESR 140 | Lightpanda |
|---|---|---|---|---|
| Entries, headers, status, redirects | yes | yes | yes | yes |
| Cookies in request headers | yes (from `ExtraInfo` events) | yes | yes | yes, but the `Cookie` value is reported one character short (nightly 9608) |
| Response bodies | yes | yes (`network.addDataCollector`, up to 200 MB in total) | no: the command does not exist | yes |
| POST bodies | yes | yes | no | yes |

Chromium discards a response body as soon as the tab navigates elsewhere, so bodies are fetched the moment each
request finishes. Nothing to do on your side, but it is why recording must be started before the navigation.

## From HAR to a direct client

1. Capture while doing the action by hand or by script.
2. `har summary -M json` to list candidate endpoints; open only those entries in the HAR.
3. Reproduce the request with `urllib`/`curl`, adding headers one by one until it works (often `Cookie`, a CSRF header,
   `Referer`, `X-Requested-With`).
4. Keep a small fixture with the response and write the parser against it; re-capture when the site changes.
