# Pitfalls, containers and troubleshooting

## First check

`crau browser doctor` shows each browser's binary and version, `HOME`, the HOME handed to browsers, the
state and cache directories and the running instances. `-v` on any command prints launch and navigation timings; a
`LaunchError` message ends with the tail of the browser's own log.

## Containers

- **`HOME=/` or unset.** Browsers write under HOME at startup; with an unwritable HOME Chromium on Debian was seen
  crashing with SIGTRAP and leaving `core` files in the working directory. The library always gives the browser a
  writable HOME, runs it in a private working directory and sets the core-dump limit to zero.
- **PID 1 that does not reap** (`sleep infinity`, `tail -f`): dead browser helpers stay as zombies. Harmless, but use
  `init: true` in Compose or `docker run --init`. "Is it still running" is decided from `/proc`, ignoring zombies.
- **Sandbox.** Chromium's sandbox needs privileges most containers lack, so `--no-sandbox` is passed unless
  `sandbox=True`.
- **`/dev/shm` too small**: `--disable-dev-shm-usage` is always passed.
- **TLS-intercepting proxies** make every HTTPS page fail with certificate errors (`curl` works because it uses the
  system store; browsers do not). Use `-k` / `insecure=True`, or install the CA into the browser profile.
- **Never `pkill -f chromium`**: the pattern also matches the shell that runs the command, and unrelated browsers.
  Stop instances with `crau browser stop NAME`; everything else is cleaned up by the library.

## Installing

`crau browser install BROWSER [-n]` (`-n` prints the plan). Per distribution: Debian uses apt for Chromium;
Ubuntu's `chromium-browser` and `firefox` packages are snap stubs that do not work in containers, so Chromium comes
from Chrome for Testing; Firefox comes from Mozilla's APT repository on both (current release, because ESR 140 lacks
BiDi data collectors and extra headers; `-c esr` if you need ESR); Alpine uses apk and cannot run Lightpanda. Without
root everything is downloaded into `~/.local/share/crau` and linked from `~/.local/bin`.
Verified by running it: Ubuntu 24.04 as root. Debian and Alpine are exercised by the Dockerfile and CI.

`pip install crau` itself may be refused on Debian and Ubuntu (PEP 668, externally managed environment): use
`pipx install crau`, a virtual environment, or `pip install --user --break-system-packages crau`.

## Symptoms

| Symptom | Likely cause |
|---|---|
| exit 3, log tail mentions a missing shared library | downloaded build without its libraries: rerun `crau browser install` as root, or install the listed `lib*` packages |
| exit 4 with a certificate error | TLS interception: `-k` |
| exit 4 `ERR_UNSAFE_PORT` | Chromium refuses some ports (1, 7, 9, 6000...) by design |
| exit 5 on a selector you can see in the HTML | it is inside an iframe or shadow root (see `cdp.md` / `bidi.md`), or the page is an error/consent screen: take a screenshot |
| empty `text` on Lightpanda | page needs an API Lightpanda lacks: use chromium |
| Firefox: "Maximum number of active sessions" | a previous client died without `session.end`: restart the instance |
| HAR entries without bodies on Firefox | ESR 140: install the current release |
| search results irrelevant on Bing | seen from a datacentre IP in September 2026; prefer DuckDuckGo (the default) |
