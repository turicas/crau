# Library reference

`crau.browser` is an asyncio package (Python >= 3.10) that uses only the standard library: it works with a bare
`pip install crau`, without the archiving extras.

```python
from crau.browser import Browser, Response, Tab, instances
from crau.browser import BrowserError, LaunchError, NavigationError, WaitTimeout
from crau.browser import JsError, UnsupportedError, ProtocolError
```

## Browser

`Browser(browser="chromium", *, headless=True, stealth=True, profile=None, viewport=(1440, 900), user_agent=None,
cookies=None, headers=None, extra_args=(), prefs=None, binary=None, sandbox=False, insecure=False, timeout=30.0)`

| Option | Meaning |
|---|---|
| `browser` | `chromium`, `firefox` or `lightpanda` |
| `profile` | directory that persists cookies, logins and cache; default: temporary, deleted on close |
| `stealth` | hide `navigator.webdriver`; headless Chromium also sends a regular Chrome User-Agent |
| `cookies` | list of cookie dicts, or a path to `cookies.txt` (Netscape) or JSON |
| `headers` | dict, or a path to a JSON object / `Name: value` lines; sent with every request |
| `extra_args` | extra command-line arguments for the browser, e.g. `["--lang=pt-BR"]` |
| `prefs` | Firefox preferences written to `user.js`, e.g. `{"intl.accept_languages": "pt-BR"}` |
| `binary` | executable; otherwise `CRAU_<BROWSER>`, then `PATH`, then `~/.local/bin` |
| `sandbox` | keep Chromium's sandbox on (off by default: it does not start in most containers) |
| `insecure` | accept invalid TLS certificates |
| `host`, `port` | debugging address; by default the browser picks a free port itself |
| `timeout` | seconds for launch, each navigation and each wait |

Use it as `async with Browser(...) as browser:` (or `await browser.start()` / `await browser.close()`).

- `await browser.new_tab(url=None, **goto_options)`: new tab, optionally navigating; Lightpanda allows one tab
- `await browser.tabs()`: open tabs in browser order, including ones opened by pages and the initial blank tab
- `await browser.set_cookies(cookies_or_path)` / `await browser.cookies()`: cookie dict keys: `name`, `value`, `domain`
  or `url`, `path`, `secure`, `http_only`, `expires`, `same_site`
- `await browser.set_headers(headers_or_path)`: replaces the previous extra headers
- `browser.version`: e.g. `Mozilla Firefox 156.0`
- `Browser.attach(name)`: drive a named instance; closing only detaches
- `Browser.connect(endpoint, browser="chromium")`: drive a browser that is already running, given its WebSocket
  endpoint (`webSocketDebuggerUrl` for CDP, `ws://host:port/session` for Firefox); closing only disconnects
- `await browser.start_instance(name)`: launch and register under `name`; the browser survives this process

`instances.list_all()`, `instances.load(name)`, `instances.stop(name)` manage named instances from Python.

## Tab

Targets are CSS selectors, `text=Visible text` (links, buttons and labels first, exact match before partial) or
`label=Field label` (matches `<label>`, `placeholder`, `aria-label`, `name`, `id`).

- `await tab.goto(url, *, wait_until="load", wait_selector=None, wait_js=None, wait_idle=False, scroll=False, wait_ms=0,
  timeout=None)`: returns `Response(url, status, headers, ok)` of the main document or `None`; raises `NavigationError`
  when the page cannot be loaded, not on HTTP errors
- `await tab.wait(*, selector=None, js=None, idle=False, scroll=False, ms=0, timeout=None)`: `selector`/`js` raise
  `WaitTimeout`; `idle` (no requests in flight for 500 ms) and `scroll` are best effort, capped at 15 s
- `await tab.settle(timeout=15.0, *, idle=0.5, minimum=1.0, scroll=False)`: what `wait(idle=..., scroll=...)` runs;
  returns `{settle_seconds, scroll_steps, pending}`
- `await tab.evaluate(expression, *, await_promise=True)`: plain data back (`undefined`/`null` -> `None`); `JsError` on
  exceptions; statements are fine: `"const a = 1; a + 1"`
- `await tab.call_function("(a, b) => ...", *args)`: arguments are JSON-encoded
- `await tab.html()`, `text()`, `title()`, `url()`, `info()`: `info()` gives `url` and `title` without running script
  (works on privileged pages)
- `await tab.outline()`: `{url, title, items: [{role, name, selector, level?, href?, type?, value?, checked?,
  disabled?}]}`
- `await tab.exists(target)`: no waiting
- `await tab.click(target, *, timeout=None)`: waits, scrolls into view, real mouse events (JS click on Lightpanda)
- `await tab.fill(target, value, *, timeout=None)`: inputs, textareas, selects (by value or label), checkboxes/radios
  (`True`/`False`), contenteditable; fires `input` and `change`
- `await tab.type(target, text)` / `await tab.press(key)`: real keystrokes; keys: `Enter`, `Tab`, `Escape`, `Backspace`,
  `Delete`, arrows, `Home`, `End`, `PageUp`, `PageDown`, `Space` or one character
- `await tab.screenshot(path=None, *, full_page=False, selector=None, image_format=None, quality=None)`: returns bytes;
  format from the extension (`.png`, `.jpg`)
- `await tab.pdf(path=None, *, landscape=False, background=True, header=None, footer=None)`: templates are Chromium only
  (classes `pageNumber`, `totalPages`, `date`, `title`, `url`)
- `await tab.set_viewport(width, height)`
- `await tab.start_recording()` / `await tab.stop_recording()` / `await tab.har()`: see `network-capture.md`
- `tab.console`, `tab.dialogs`, `tab.response`: console entries `{level, text}`; dialogs are accepted automatically and
  listed
- `await tab.cdp(method, params)` / `await tab.bidi(method, params)`: raw protocol; BiDi does not add `context`: use
  `tab.id`
- `await tab.activate()`, `await tab.close()`, `tab.id`

## Errors

All derive from `BrowserError`: `LaunchError` (binary missing, early exit, endpoint never opened; the message carries
the browser's log tail), `NavigationError`, `WaitTimeout` (also a `TimeoutError`), `JsError`, `UnsupportedError`,
`ProtocolError` (`.method`, `.code`, `.message` from the browser).

## Behaviour worth knowing

- Every launch uses port 0, its own process group, a private working directory, a writable `HOME` and a zero core-dump
  limit; closing waits for a clean exit so profiles are flushed, then kills whatever is left.
- `SIGTERM` and `Ctrl+C` run the cleanup (exit codes 143 and 130 in the CLIs).
- Firefox allows one BiDi session at a time. `close()` always ends it; if a client dies without closing, the instance
  answers "Maximum number of active sessions" until it is restarted.
- Tab ids are not stable across Firefox sessions: find tabs by position or by `info()["url"]`.
- State directories: `$XDG_STATE_HOME/crau` (instances) and `$XDG_CACHE_HOME/crau` (logs,
  temporary profiles), falling back to `~/.local/state`, `~/.cache` and then the temp dir when unset or unwritable.
