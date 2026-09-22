# Verifying crau

How to convince yourself that everything works, from the quickest check to the full suite, and an honest list of what
was and was not verified when the browser layer was merged (September 2026).

## 1. With Docker (recommended)

```bash
make build        # Python 3.14 on Debian trixie + Chromium, Firefox and Lightpanda via `crau browser install`
make start
make doctor       # three browsers listed with their versions
make smoke        # end-to-end checks through the public CLI; ends with "N passed, 0 failed"
make test         # the whole suite, real browsers included
make stop
```

`make build` is itself a test: the image installs the browsers with `crau browser install all`, so a successful build
means the Debian route works. Inside the image `REQUIRE_BROWSERS=1` is set, so a missing browser fails the tests
instead of skipping them.

```bash
make test-unit                                                       # everything that needs no browser
make test TEST_ARGS="tests/browser/integration/test_browser_state.py -k har -x"
make test TEST_ARGS="-k firefox"
make smoke SMOKE_ARGS="-b chromium -v"                               # one browser, printing every command
make smoke SMOKE_ARGS="--online"                                     # also loads https://example.com
```

## 2. Without Docker

```bash
pip install -r requirements-development.txt && pip install --no-deps -e .
crau browser install all          # as root: packages; as a user: downloads into ~/.local; -n shows the plan
crau browser doctor
pytest                            # browser tests are skipped for browsers that are not installed
python tests/browser/smoke.py
PYTHONPATH=. python scripts/smoke_test.py --backend all --target-url http://127.0.0.1:8765/   # with the site below
```

Behind a TLS-intercepting proxy, `tests/browser/smoke.py --online` needs `-k`.

## 3. By hand

```bash
python tests/browser/testsite.py 8765 &                              # the local site the tests use
crau page text http://127.0.0.1:8765/final                           # -> Final page
crau page js http://127.0.0.1:8765/form "document.title" -b firefox  # -> "Form"
crau page outline http://127.0.0.1:8765/form                         # fields and buttons with selectors
crau page screenshot http://127.0.0.1:8765/ /tmp/full.png -f && file /tmp/full.png
crau har capture http://127.0.0.1:8765/ /tmp/site.har -I && crau har summary /tmp/site.har -m POST
crau browser start demo && crau tab open demo http://127.0.0.1:8765/form
crau page js tab:1 "document.title" -i demo && crau browser stop demo
crau archive /tmp/site.warc.gz http://127.0.0.1:8765/redirect --backend chromium --max-depth 0 && crau list /tmp/site.warc.gz
python examples/browser_form_flow.py http://127.0.0.1:8765 /tmp/flow -b lightpanda
kill %1
```

Worth checking with your own eyes: `/tmp/full.png` is the whole page; `/tmp/site.har` has the POST body and the JSON
response; the WARC lists both `/redirect` (the 302) and `/final`; after each command no browser process is left and no
`core` file appeared.

## What the suites cover

| Where | Needs a browser | What it proves |
|---|---|---|
| `tests/test_*.py` | only `test_browser_fetchers_real.py` | the archiver: HTTP fetcher, crawler, cache, WARC/HAR models, CLI, optional dependencies, and the browser fetchers (scripted fake CDP/BiDi browsers, broken-browser scripts, and real browsers through `crau archive`) |
| `tests/browser/unit/` | no | WebSocket framing, protocol connections against an in-process fake browser, launch commands, directories with broken `HOME`/XDG, instances, cookie and header files, HAR building and summary, CLI parsing and exit codes, install plans for Debian/Ubuntu/Alpine/unprivileged, the agent skill |
| `tests/browser/integration/` | yes | every public method on the three browsers against `tests/browser/testsite.py`, every CLI command, signals, TLS, examples, and every raw-protocol recipe of the skill's references |
| `tests/browser/smoke.py` | yes | the public commands exactly as an agent runs them |
| `scripts/smoke_test.py` | yes | the archiver end to end (WARC, list/extract, the three HAR modes) on every backend |

## What was verified, and where

In an Ubuntu 24.04 x86_64 sandbox as root, Python 3.12, Chrome for Testing 153.0.8010.52, Firefox 156.0 (Mozilla APT)
and Lightpanda nightly 9610, with the `websockets` package uninstalled:

- the whole suite, the browser smoke test and `scripts/smoke_test.py --backend all` against the local site;
- `crau browser install all` for real; a wheel built from the tree has no `Requires-Dist` outside extras and ships
  `page.js` and the skill documents;
- `import crau`, `crau --help` and the browser commands with every third-party import blocked.

Not verified, because the sandbox had no Docker and a single distribution: building the `Dockerfile` and everything
that depends on it (`make *`, the `container` CI job); `crau browser install` on Debian and Alpine; the browsers Debian
ships (the reported crash of Debian's Chromium with `HOME=/` could not be reproduced with Chrome for Testing, so
`test_browser_leaves_no_process_no_core_file_and_no_temp_dir` only becomes meaningful there); Python 3.10 and 3.14;
`aarch64`; the GitHub Actions workflow itself; `crau play`.
