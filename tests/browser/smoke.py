#!/usr/bin/env python3
"""Smoke test: exercise the browser commands end to end, through the public CLI, against the bundled local site.

No pytest needed. For every browser found (or given with -b) it runs the real commands an agent would run and prints
one PASS/FAIL line per check, with timings. Nothing leaves the machine unless --online is given.

exit codes: 0 every check passed, 1 at least one failed, 2 invalid command line, 3 no browser found, 130 interrupted
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "browser")]
BROWSERS = ("chromium", "firefox", "lightpanda")
CLI = [sys.executable, "-m", "crau"]


class Runner:
    def __init__(self, verbose: bool, insecure: bool) -> None:
        self.verbose, self.insecure = verbose, insecure
        self.results: list[tuple[str, bool, float, str]] = []

    def run(self, *command, env=None, timeout=180) -> subprocess.CompletedProcess:
        command = [str(part) for part in command]
        if self.verbose:
            print("    $ " + " ".join(command), file=sys.stderr)
        environment = {**os.environ, **(env or {})}
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=environment, cwd=ROOT)

    def check(self, name: str, function) -> None:
        started = time.perf_counter()
        try:
            function()
            passed, details = True, ""
        except Exception as exc:  # a smoke test reports every kind of failure instead of stopping at the first
            passed, details = False, f"{type(exc).__name__}: {exc}".strip()[:600]
        elapsed = time.perf_counter() - started
        self.results.append((name, passed, elapsed, details))
        print(
            f"{'PASS' if passed else 'FAIL'}  {elapsed:6.2f}s  {name}" + (f"\n      {details}" if details else ""),
            flush=True,
        )


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    expect(result.returncode == 0, f"exit {result.returncode}: {result.stderr.strip()[-400:]}")
    return result


def browser_checks(runner: Runner, name: str, site_url: str, work: Path) -> None:
    full = name != "lightpanda"
    common = ["-b", name]

    def html():
        result = ok(runner.run(*CLI, "page", "html", site_url + "/", "-s", "#late", *common))
        expect('<div id="late">hydrated</div>' in result.stdout, "late content missing from the rendered HTML")
        expect(result.stderr == "", f"success must be silent on stderr, got: {result.stderr[:200]}")

    def javascript():
        result = ok(
            runner.run(*CLI, "page", "js", site_url + "/form", "({title: document.title, wd: navigator.webdriver})", *common)
        )
        expect(json.loads(result.stdout) == {"title": "Form", "wd": False}, f"unexpected value: {result.stdout}")

    def outline():
        result = ok(runner.run(*CLI, "page", "outline", site_url + "/form", *common))
        expect("textbox\tUser name\t#user" in result.stdout, "outline does not list the user field")

    def exit_codes():
        expect(runner.run(*CLI, "page", "html", "http://127.0.0.1:9/", "-t", "10", *common).returncode == 4, "unreachable != 4")
        expect(
            runner.run(*CLI, "page", "html", site_url + "/final", "-s", "#never", "-t", "2", *common).returncode == 5,
            "wait != 5",
        )
        expect(runner.run(*CLI, "page", "js", site_url + "/final", "nope.x", *common).returncode == 6, "JS error != 6")
        expect(runner.run(*CLI, "page", "text", site_url + "/missing", "-x", *common).returncode == 8, "HTTP 404 with -x != 8")

    def headers_and_cookies():
        cookie_file = work / f"{name}-cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsmoke\tyes\n")
        fetch_echo = "fetch('/echo').then(r => r.json())"
        result = ok(
            runner.run(*CLI, "page", "js", site_url + "/final", fetch_echo, "-H", "X-Smoke: 1", "-c", cookie_file, *common)
        )
        echoed = json.loads(result.stdout)
        expect(echoed.get("x-smoke") == "1" and "smoke=yes" in echoed.get("cookie", ""), f"server saw: {echoed}")

    def har():
        har_file = work / f"{name}.har"
        ok(runner.run(*CLI, "har", "capture", site_url + "/", site_url + "/redirect", har_file, "-I", *common))
        entries = json.loads(har_file.read_text())["log"]["entries"]
        post = [entry for entry in entries if entry["request"]["method"] == "POST"]
        expect(
            bool(post) and post[0]["request"]["postData"]["text"] == "field=value&other=1", "POST body missing in HAR"
        )
        expect("received" in post[0]["response"]["content"].get("text", ""), "response body missing in HAR")
        summary = ok(runner.run(*CLI, "har", "summary", har_file, "-m", "POST"))
        expect(
            '"field": "string"' in summary.stdout and "field=value" not in summary.stdout,
            "summary must show shapes only",
        )

    def screenshot_and_pdf():
        shot, pdf = work / f"{name}-full.png", work / f"{name}.pdf"
        ok(runner.run(*CLI, "page", "screenshot", site_url + "/", shot, "-f", "-V", "800x600", *common))
        data = shot.read_bytes()
        expect(
            data[:4] == b"\x89PNG" and int.from_bytes(data[20:24], "big") > 2000,
            "full-page PNG is not taller than 2000px",
        )
        ok(runner.run(*CLI, "page", "pdf", site_url + "/final", pdf, *common))
        expect(pdf.read_bytes()[:5] == b"%PDF-", "not a PDF")

    def instance():
        env = {"XDG_STATE_HOME": str(work / f"{name}-state")}
        ok(runner.run(*CLI, "browser", "start", "smoke", *common, env=env))
        try:
            index = ok(runner.run(*CLI, "tab", "open", "smoke", site_url + "/form", env=env)).stdout.strip()
            ok(
                runner.run(
                    *CLI,
                    "page",
                    "js",
                    f"tab:{index}",
                    "document.querySelector('#user').value = 'kept'; 1",
                    "-i",
                    "smoke",
                    env=env,
                )
            )
            kept = ok(
                runner.run(*CLI, "page", "js", f"tab:{index}", "document.querySelector('#user').value", "-i", "smoke", env=env)
            )
            expect(json.loads(kept.stdout) == "kept", "state did not survive between commands")
        finally:
            ok(runner.run(*CLI, "browser", "stop", "smoke", env=env))

    def library_python():
        result = ok(
            runner.run(sys.executable, ROOT / "examples" / "browser_form_flow.py", site_url, work / f"{name}-py", *common)
        )
        expect(json.loads(result.stdout)["title"] == "signed-in", "form flow did not sign in")

    def web_search():
        result = ok(runner.run(*CLI, "search", "smoke", "-J", "-u", site_url + "/ddg?q={query}", *common))
        expect(json.loads(result.stdout)[0]["url"] == "https://example.org/one?a=1", "redirect link was not decoded")

    def online():
        extra = ["-k"] if runner.insecure else []
        result = ok(runner.run(*CLI, "page", "js", "https://example.com/", "document.title", *extra, *common))
        expect("Example" in result.stdout, f"unexpected title: {result.stdout}")

    checks = [
        ("html + wait", html),
        ("js + stealth", javascript),
        ("outline", outline),
        ("exit codes", exit_codes),
        ("headers + cookie file", headers_and_cookies),
        ("har capture + summary", har),
        ("web search page", web_search),
        ("library example (python)", library_python),
    ]
    if full:
        checks += [("screenshot + pdf", screenshot_and_pdf), ("named instance across commands", instance)]
    if runner.online:
        checks.append(("online: example.com", online))
    for label, function in checks:
        runner.check(f"{name}: {label}", function)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "-b", "--browser", metavar="name", action="append", choices=BROWSERS,
        help=f"Browser to test (repeatable). Options: {', '.join(BROWSERS)} (default: every browser found)",
    )  # fmt: skip
    parser.add_argument("-o", "--online", action="store_true", help="Also load https://example.com (needs internet)")
    parser.add_argument("-k", "--insecure", action="store_true", help="With --online: accept invalid TLS certificates")
    parser.add_argument("-K", "--keep", action="store_true", help="Keep the output directory and print its path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every command before running it")
    return parser


def main() -> None:
    args = create_parser().parse_args()
    from crau.browser.errors import LaunchError
    from crau.browser.launch import find_binary
    from testsite import Site

    runner = Runner(args.verbose, args.insecure)
    runner.online = args.online
    names = []
    for name in args.browser or BROWSERS:
        try:
            find_binary(name)
            names.append(name)
        except LaunchError as exc:
            print(f"SKIP  {name}: {exc}", flush=True)
    if not names:
        sys.exit(3)
    work = Path(tempfile.mkdtemp(prefix="crau-smoke-"))
    try:
        doctor = runner.run(*CLI, "browser", "doctor")
        print(doctor.stdout.rstrip(), flush=True)
        with Site() as site:
            for name in names:
                browser_checks(runner, name, site.url, work)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    finally:
        if args.keep:
            print(f"output kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    failed = [name for name, passed, _, _ in runner.results if not passed]
    total = sum(elapsed for _, _, elapsed, _ in runner.results)
    print(f"\n{len(runner.results) - len(failed)} passed, {len(failed)} failed in {total:.1f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
