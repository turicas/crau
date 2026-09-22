# AGENTS.md

> crau is a Web archiver and crawler (WARC/HAR) whose browser layer, `crau.browser`, doubles as a dependency-free
> browser automation library and CLI for AI agents.

## Commands

- In the `dev` container (three browsers installed): `make build`, `make start`, `make test`, `make smoke`. One test:
  `make test TEST_ARGS="tests/browser/integration/test_browser_state.py -k har -x"`. Without a browser: `make test-unit`.
- Outside the container: `pytest`; browser tests are skipped when a binary is missing, and `REQUIRE_BROWSERS=1` (set
  in the image) turns that into a failure. Formatting: `make fix-imports`. Lint: `ruff check --select F crau tests`.

## Conventions

- `pip install crau` installs no dependency. Anything under `crau/browser`, `crau/skill`, `crau/__init__.py` and the
  import path of `crau --help` must work with the standard library alone; third-party imports belong to the archiving
  code and are loaded on first use (`load_archive_modules` in the CLI). `tests/test_optional_dependencies.py` and
  `test_help_imports_neither_asyncio_nor_the_archiving_dependencies` guard this.
- No `websockets`, `requests`, Playwright, Puppeteer or Selenium: the WebSocket client, protocol clients, launcher and
  HAR recorder live in `crau.browser`. The browser fetchers are thin adapters over `Browser` and `Tab`: fix browser
  behaviour in `crau.browser`, never in a fetcher.
- `crau.browser` is asyncio end to end. Browsers are started with `subprocess.Popen`, not
  `asyncio.create_subprocess_exec`: asyncio kills its children when the loop closes, which would take named instances
  down with the command that started them.
- Every claim about browser behaviour gets a test against a real browser, with browser and version recorded next to
  the workaround. Do not code around a quirk you have not reproduced.
- JavaScript that runs inside pages goes in `page.js` and nowhere else.
- The three browsers behave the same through the public API. When one cannot do something, raise `UnsupportedError`
  with the reason instead of returning something approximate.
- CLI: data on stdout, status on stderr, silent success, a short flag for every long one, documented exit codes.
  Browser commands keep their own `-q`/`-v` destinations so they do not overwrite crau's global flags. Secrets never
  travel as command-line arguments.
- HAR and WARC output is complete: never filter, truncate or redact headers, cookies or bodies.
- Tests assert behaviour seen from outside (what the server received, what the file contains, the exit code), not the
  protocol messages the library happened to send. Scripted fake browsers must emit what real browsers emit.
- Commit titles: English, imperative, no body.

## Domain vocabulary

- "Instance": a browser process registered under a name in the state directory, which outlives the command that
  started it (`crau browser start NAME`). "Attach" is connecting to one; "connect" is driving any endpoint.
- "Target": how a caller points at an element: a CSS selector, `text=...` or `label=...`.
- "Stealth": the defaults that hide automation fingerprints. On by default.
- "Settle": waiting for the network to go idle, optionally scrolling to the bottom first. Best effort by design.
- "Backend": in `crau.browser`, the per-protocol half (CDP or BiDi) behind `Browser`/`Tab`; in the archiver, the
  fetcher chosen with `--backend`.
- "Hop": one request in a redirect chain. Network records, and therefore transactions, exist per hop.
- "Transaction": a request/response pair as the archiver stores it (`NetworkTransaction`), built from a network record.

## Adding a capability to `Tab`

1. Reproduce it by hand on the three browsers with `tab.cdp()` / `tab.bidi()`; note what each one cannot do.
2. Write the integration test first, parametrized with the `browser_name` fixture (`full_browser_name` when
   Lightpanda cannot support it, plus a test that it raises `UnsupportedError`).
3. Add the primitive to both backends with the same signature, then the public method on `Tab`.
4. Expose it in the CLI only if it is useful as a one-shot command; show it in the skill if an agent needs it.
5. Document it in the skill's library reference; raw-protocol recipes go in the CDP/BiDi references together with a
   test in `test_browser_reference_recipes.py`.
6. `make test && make smoke`.

## References

- `TESTING.md`: how to verify a checkout, what has and has not been verified, and on which versions.
- `crau/skill/SKILL.md` and `crau/skill/references/`: what `crau skill` prints; `pitfalls.md` collects the container
  and browser traps already found (add new ones there, with the version).
