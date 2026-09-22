"""What each browser command of the `crau` CLI does.

Data goes to stdout (or the output file); status, warnings and errors go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from . import instances
from .cookies import dump_netscape
from .errors import BrowserError, JsError, LaunchError, NavigationError, UnsupportedError, WaitTimeout
from .har import summarize_har
from .launch import BROWSERS, binary_version, find_binary
from .paths import browser_home, cache_dir, state_dir
from .session import Browser

EXIT_BY_ERROR = ((LaunchError, 3), (NavigationError, 4), (WaitTimeout, 5), (JsError, 6), (UnsupportedError, 7))
HTTP_ERROR_EXIT = 8


class UsageError(Exception):
    """Arguments that argparse accepted but that do not make sense together."""


def _log(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "browser_verbose", False) or getattr(args, "verbose", 0):
        print(message, file=sys.stderr, flush=True)


def _warn(args: argparse.Namespace, message: str) -> None:
    if not (getattr(args, "browser_quiet", False) or getattr(args, "quiet", False)):
        print(f"crau: warning: {message}", file=sys.stderr, flush=True)


def _write(data: str | bytes, output: Path | None) -> None:
    if output is None:
        if isinstance(data, bytes):
            sys.stdout.buffer.write(data)
        else:
            sys.stdout.write(data if data.endswith("\n") else data + "\n")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        output.write_bytes(data)
    else:
        output.write_text(data, encoding="utf-8")


def _dump_json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _new_browser(args: argparse.Namespace) -> Browser:
    return Browser(
        args.browser,
        headless=not args.no_headless,
        stealth=not args.no_stealth,
        profile=args.profile,
        viewport=args.viewport,
        user_agent=args.user_agent,
        extra_args=args.browser_arg,
        prefs=dict(args.pref),
        binary=args.binary,
        insecure=args.insecure,
        timeout=args.timeout,
    )


async def _apply_cookies_and_headers(browser: Browser, args: argparse.Namespace) -> None:
    if args.cookies:
        await browser.set_cookies(args.cookies)
    headers = {}
    if args.headers_file:
        from .cookies import load_headers

        headers.update(load_headers(args.headers_file))
    headers.update(dict(args.header))
    if headers:
        await browser.set_headers(headers)


@asynccontextmanager
async def _session(args: argparse.Namespace):
    """Browser for this command: the named instance when --instance is given, otherwise a fresh one."""
    started = time.perf_counter()
    if args.instance:
        browser = Browser.attach(args.instance, timeout=args.timeout)
    else:
        browser = _new_browser(args)
    async with browser:
        _log(args, f"{browser.name} ready in {time.perf_counter() - started:.2f}s ({browser.version})")
        await _apply_cookies_and_headers(browser, args)
        yield browser


async def _goto(tab, url: str, args: argparse.Namespace):
    started = time.perf_counter()
    response = await tab.goto(
        url,
        wait_until=args.wait_until,
        wait_selector=args.wait_selector,
        wait_js=args.wait_js,
        wait_ms=args.wait,
        wait_idle=args.wait_idle,
        scroll=args.scroll,
    )
    status = response.status if response else "no response reported"
    _log(args, f"{url} -> {status} in {time.perf_counter() - started:.2f}s")
    return response


@asynccontextmanager
async def _page(args: argparse.Namespace, url: str):
    """Yield `(tab, http_failed)` with the page loaded, waits done and --eval scripts executed."""
    if url.startswith("tab:") and not args.instance:
        raise UsageError("tab:N needs --instance")
    async with _session(args) as browser:
        response = None
        if url.startswith("tab:"):
            tabs = await browser.tabs()
            index = url[4:]
            if not index.isdigit() or int(index) >= len(tabs):
                raise UsageError(f"No tab {index!r}: instance {args.instance!r} has tabs 0-{len(tabs) - 1}")
            tab = tabs[int(index)]
            await tab.wait(
                selector=args.wait_selector, js=args.wait_js, ms=args.wait, idle=args.wait_idle, scroll=args.scroll
            )
        else:
            tab = await browser.new_tab()
            response = await _goto(tab, url, args)
        try:
            scripts = list(args.eval)
            if args.eval_file:
                scripts.append(args.eval_file.read_text(encoding="utf-8"))
            for script in scripts:
                await tab.evaluate(script)
            failed = bool(args.fail_on_http_error and response is not None and response.status >= 400)
            if failed:
                _warn(args, f"{url} answered HTTP {response.status}")
            yield tab, failed
        finally:
            if args.console:
                for entry in tab.console:
                    print(f"console.{entry['level']}: {entry['text']}", file=sys.stderr)


# --- page commands ------------------------------------------------------------------------------------------------


async def _html(args) -> int:
    async with _page(args, args.url) as (tab, failed):
        _write(await tab.html(), args.output)
    return HTTP_ERROR_EXIT if failed else 0


async def _text(args) -> int:
    async with _page(args, args.url) as (tab, failed):
        _write(await tab.text(), args.output)
    return HTTP_ERROR_EXIT if failed else 0


async def _js(args) -> int:
    if (args.expression is None) == (args.file is None):
        raise UsageError("give either an expression or --file")
    expression = args.expression if args.file is None else args.file.read_text(encoding="utf-8")
    async with _page(args, args.url) as (tab, failed):
        _write(_dump_json(await tab.evaluate(expression)), None)
    return HTTP_ERROR_EXIT if failed else 0


def _outline_line(item: dict) -> str:
    role = f"{item['role']}[{item['level']}]" if "level" in item else item["role"]
    extras = [f"{key}={item[key]}" for key in ("type", "href", "src", "value", "checked", "disabled") if key in item]
    return "\t".join([role, item["name"], item["selector"], *extras])


async def _outline(args) -> int:
    async with _page(args, args.url) as (tab, failed):
        outline = await tab.outline()
    if args.json:
        _write(_dump_json(outline), None)
    else:
        lines = [f"# {outline['title']}", f"# {outline['url']}", *map(_outline_line, outline["items"])]
        _write("\n".join(lines), None)
    return HTTP_ERROR_EXIT if failed else 0


async def _screenshot(args) -> int:
    if args.output.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise UsageError(f"unsupported image extension {args.output.suffix!r} (use .png, .jpg or .jpeg)")
    async with _page(args, args.url) as (tab, failed):
        await tab.screenshot(args.output, full_page=args.full_page, selector=args.selector, quality=args.quality)
    return HTTP_ERROR_EXIT if failed else 0


async def _pdf(args) -> int:
    async with _page(args, args.url) as (tab, failed):
        await tab.pdf(
            args.output,
            landscape=args.landscape,
            background=not args.no_background,
            header=args.header_template,
            footer=args.footer_template,
        )
    return HTTP_ERROR_EXIT if failed else 0


async def _cookies(args) -> int:
    async with _page(args, args.url) as (tab, failed):
        cookies = await tab.browser.cookies()
    _write(_dump_json(cookies) if args.json else dump_netscape(cookies), args.output)
    return HTTP_ERROR_EXIT if failed else 0


# --- har ----------------------------------------------------------------------------------------------------------


async def _har_capture(args) -> int:
    failed = False
    async with _session(args) as browser:
        tab = await browser.new_tab()
        if not await tab.start_recording():
            _warn(args, f"{browser.version} cannot provide response bodies: entries will have none")
        for url in args.url:
            response = await _goto(tab, url, args)
            failed = failed or bool(args.fail_on_http_error and response is not None and response.status >= 400)
            for script in args.eval:
                await tab.evaluate(script)
        har = await tab.har()
    _write(_dump_json(har), args.output)
    _log(args, f"{len(har['log']['entries'])} entries written to {args.output}")
    return HTTP_ERROR_EXIT if failed else 0


def _har_summary(args) -> int:
    har = json.loads(args.har.read_text(encoding="utf-8"))
    rows = summarize_har(har, url_filter=args.url_filter, method=args.method, mime_filter=args.mime)
    if args.json:
        _write(_dump_json(rows), None)
        return 0
    lines = []
    for row in rows:
        lines.append(f"[{row['entry']}] {row['method']} {row['status']} {row['endpoint']}")
        lines.append(f"    response: {row['mime_type'] or '-'}, {row['size']} bytes")
        for label, key in (("query", "query"), ("request headers", "request_headers")):
            if row[key]:
                lines.append(f"    {label}: {', '.join(row[key])}")
        for label, key in (("request body", "request_body"), ("response body", "response_body")):
            if row[key] is not None:
                lines.append(f"    {label}: {json.dumps(row[key], ensure_ascii=False)}")
    _write("\n".join(lines) if lines else "", None)
    return 0


# --- instances and tabs -------------------------------------------------------------------------------------------


async def _instance_start(args) -> int:
    browser = _new_browser(args)
    instance = await browser.start_instance(args.name)
    try:
        await _apply_cookies_and_headers(browser, args)
    finally:
        await browser.close()
    _log(args, f"{instance.name}: {instance.version}, pid {instance.pid}, {instance.endpoint}")
    return 0


def _instance_stop(args) -> int:
    instance = instances.stop(args.name)
    _log(args, f"{instance.name}: stopped pid {instance.pid}")
    return 0


def _instance_list(args) -> int:
    found = instances.list_all()
    if args.json:
        _write(_dump_json([vars(instance) for instance in found]), None)
    elif found:
        _write("\n".join(f"{item.name}\t{item.browser}\t{item.pid}\t{item.version}" for item in found), None)
    return 0


async def _tab_list(args) -> int:
    async with Browser.attach(args.instance) as browser:
        rows = [{"index": index, **(await tab.info())} for index, tab in enumerate(await browser.tabs())]
    if args.json:
        _write(_dump_json(rows), None)
    elif rows:
        _write("\n".join(f"{row['index']}\t{row['url']}\t{row['title']}" for row in rows), None)
    return 0


async def _tab_open(args) -> int:
    async with Browser.attach(args.instance) as browser:
        tab = await browser.new_tab(args.url)
        index = (await browser.tabs()).index(tab)
    _write(str(index), None)
    return 0


async def _tab_close(args) -> int:
    async with Browser.attach(args.instance) as browser:
        tabs = await browser.tabs()
        if not 0 <= args.index < len(tabs):
            raise UsageError(f"No tab {args.index}: instance {args.instance!r} has tabs 0-{len(tabs) - 1}")
        await tabs[args.index].close()
    return 0


# --- doctor -------------------------------------------------------------------------------------------------------


async def _doctor(args) -> int:
    report: dict[str, object] = {"python": platform.python_version(), "browsers": {}}
    for name in BROWSERS:
        try:
            binary = find_binary(name)
            report["browsers"][name] = {"binary": str(binary), "version": await binary_version(binary)}
        except BrowserError as exc:
            report["browsers"][name] = {"error": str(exc)}
    report["home"] = os.environ.get("HOME", "")
    report["browser_home"] = str(browser_home())
    report["state_dir"] = str(state_dir())
    report["cache_dir"] = str(cache_dir())
    report["instances"] = [instance.name for instance in instances.list_all()]
    if args.json:
        _write(_dump_json(report), None)
    else:
        lines = [f"python\t{report['python']}"]
        for name, details in report["browsers"].items():
            lines.append(
                f"{name}\t" + (f"{details['binary']}\t{details['version']}" if "binary" in details else "MISSING")
            )
        lines += [f"{key}\t{report[key]}" for key in ("home", "browser_home", "state_dir", "cache_dir")]
        lines.append("instances\t" + (", ".join(report["instances"]) or "-"))
        _write("\n".join(lines), None)
    usable = any("binary" in details for details in report["browsers"].values())
    return 0 if usable else 3


# --- skill --------------------------------------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent / "skill"


def _skill(args) -> int:
    """Print the agent skill shipped with this version, so the instructions always match the installed commands."""
    references = {path.stem: path for path in sorted((SKILL_DIR / "references").glob("*.md"))}
    if args.list:
        _write("\n".join(references), None)
        return 0
    if args.reference:
        name = args.reference.removesuffix(".md")
        if name not in references:
            raise UsageError(f"no reference named {args.reference!r}. Options: {', '.join(references)}")
        _write(references[name].read_text(encoding="utf-8"), None)
        return 0
    _write((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"), None)
    return 0


def _search(args) -> int:
    from . import search

    return search.run(args)


def _install(args) -> int:
    from . import install

    return install.run(args)


HANDLERS = {
    "page html": _html, "page text": _text, "page js": _js, "page outline": _outline, "page screenshot": _screenshot,
    "page pdf": _pdf, "page cookies": _cookies, "har capture": _har_capture, "har summary": _har_summary,
    "browser start": _instance_start, "browser stop": _instance_stop, "browser status": _instance_list,
    "browser doctor": _doctor, "browser install": _install,
    "tab list": _tab_list, "tab open": _tab_open, "tab close": _tab_close,
    "search": _search, "skill": _skill,
}  # fmt: skip


def run(args: argparse.Namespace) -> int:
    """Execute the parsed command and translate failures into the documented exit codes."""
    handler = HANDLERS[f"{args.command} {args.action}" if hasattr(args, "action") else args.command]
    try:
        result = handler(args)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result
    except KeyboardInterrupt:
        print("\ncrau: interrupted", file=sys.stderr)
        return 130
    except UsageError as exc:
        print(f"crau: error: {exc}", file=sys.stderr)
        return 2
    except BrowserError as exc:
        print(f"crau: error: {exc}", file=sys.stderr)
        return next((code for kind, code in EXIT_BY_ERROR if isinstance(exc, kind)), 1)
    except (OSError, ValueError) as exc:
        print(f"crau: error: {exc}", file=sys.stderr)
        return 1
