# Lightpanda

A headless browser written from scratch in Zig: JavaScript and DOM, no rendering. It is in beta and fast-moving;
everything below was measured on nightly 9608/9610 (September 2026).

| Works | Does not |
|---|---|
| `html`, `text`, `js`, `outline`, `cookies`, `har capture` (bodies and POST data included) | real screenshots: `Page.captureScreenshot` returns a 1920x1080 PNG with text only |
| waits, `fill`, `click` (done in JS), extra headers, cookies, `--insecure` | PDF (`UnsupportedError`) |
| starts in about 50 ms | tabs: one page per connection; a second `new_tab()` raises `UnsupportedError` |
| | named instances: each connection gets a fresh browser, no state survives a reconnect |
| | real mouse input: layout boxes are made up and mouse events hit nothing |

Quirks seen:
- No named form access (`form.fieldName` is `undefined`); pages that rely on it break silently.
- `querySelectorAll("a, b")` returns matches grouped by selector instead of document order (`outline()` re-sorts).
- The `Cookie` request header in CDP events is one character short, although the server receives it whole.
- Uncaught page errors are not reported through `Runtime.exceptionThrown`, so `tab.console` stays empty for them.
- `Accessibility.getFullAXTree` without parameters answers `InvalidParams`.
- The User-Agent is `Lightpanda/1.0`; `--user-agent` refuses any value containing `Mozilla`, so stealth does not apply.
- Telemetry is on upstream; the library sets `LIGHTPANDA_DISABLE_TELEMETRY=true` unless you set it yourself.

Without this skill's library, `lightpanda fetch --dump html|markdown URL` prints a page directly, and the CDP command
`LP.getMarkdown` returns the page as Markdown.

When a page comes back empty or half-rendered, retry with chromium before debugging: unimplemented Web APIs are the
usual cause. The binary is glibc-only (no Alpine).
