"""Browser commands of the `crau` command line: `browser`, `tab`, `page`, `har`, `search` and `skill`.

View layer only: parsing, output and exit codes; the work is done by the library.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

BROWSER_CHOICES = ("chromium", "firefox", "lightpanda")
WAIT_UNTIL_CHOICES = ("load", "domcontentloaded", "none")
EXIT_CODES = """exit codes:
  0    success
  1    other browser or protocol error
  2    invalid command line
  3    browser missing or failed to start
  4    page could not be loaded (DNS, refused connection...)
  5    wait condition timed out
  6    JavaScript raised an exception
  7    not supported by this browser
  8    HTTP status >= 400 (only with --fail-on-http-error)
  130  interrupted (Ctrl+C)"""
URL_HELP = "URL to open, or tab:N to use tab N of --instance as it is (no navigation)"
COMMANDS = ("browser", "tab", "page", "har", "search", "skill")


def parse_viewport(value: str) -> tuple[int, int]:
    width, separator, height = value.lower().partition("x")
    if not separator or not width.isdigit() or not height.isdigit() or int(width) < 1 or int(height) < 1:
        raise argparse.ArgumentTypeError(f"Invalid viewport {value!r} (expected WIDTHxHEIGHT, e.g. 1440x900)")
    return int(width), int(height)


def parse_header_argument(value: str) -> tuple[str, str]:
    name, separator, content = value.partition(":")
    if not separator or not name.strip() or " " in name.strip():
        raise argparse.ArgumentTypeError(f"Invalid header {value!r} (expected 'Name: value')")
    return name.strip(), content.strip()


def parse_pref(value: str) -> tuple[str, object]:
    name, separator, content = value.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"Invalid preference {value!r} (expected NAME=VALUE)")
    content = content.strip()
    if content.lower() in ("true", "false"):
        return name.strip(), content.lower() == "true"
    return name.strip(), int(content) if content.lstrip("-").isdigit() else content


def existing_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"File not found: {value}")
    return path


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive number, got {value!r}")
    return number


def _verbosity_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_mutually_exclusive_group()
    # own destinations: a subparser default would otherwise overwrite crau's global --quiet/--verbose
    group.add_argument("-q", "--quiet", dest="browser_quiet", action="store_true", help="Do not print warnings")
    group.add_argument(
        "-v", "--verbose", dest="browser_verbose", action="store_true", help="Print what is being done to stderr"
    )
    return parent


def _browser_parent() -> argparse.ArgumentParser:
    env = os.environ.get
    parent = argparse.ArgumentParser(add_help=False, parents=[_verbosity_parent()])
    group = parent.add_argument_group("browser")
    group.add_argument(
        "-b", "--browser", metavar="name", choices=BROWSER_CHOICES, default=env("CRAU_BROWSER", "chromium"),
        help=f"Browser to launch. Options: {', '.join(BROWSER_CHOICES)} (default: chromium or CRAU_BROWSER)",
    )  # fmt: skip
    group.add_argument("-i", "--instance", metavar="name", help="Use this running browser (see 'crau browser start') instead of launching")
    group.add_argument(
        "-p", "--profile", metavar="dir", type=Path, default=env("CRAU_PROFILE") or None,
        help="Persistent profile directory, keeps logins (default: temporary profile, or CRAU_PROFILE)",
    )  # fmt: skip
    group.add_argument("-c", "--cookies", metavar="file", type=existing_file, help="Cookie file: cookies.txt or JSON")
    group.add_argument(
        "-H", "--header", metavar="header", type=parse_header_argument, action="append", default=[],
        help="Extra request header 'Name: value' (repeatable). Visible in the process list: prefer --headers-file "
        "for secrets",
    )  # fmt: skip
    group.add_argument(
        "-F",
        "--headers-file",
        metavar="file",
        type=existing_file,
        help="Headers file: JSON object or 'Name: value' lines",
    )
    group.add_argument(
        "-V", "--viewport", metavar="WxH", type=parse_viewport, default=(1440, 900), help="Default: 1440x900"
    )
    group.add_argument("-u", "--user-agent", metavar="ua", help="Override the User-Agent")
    group.add_argument("-S", "--no-stealth", action="store_true", help="Keep the browser's automation fingerprints")
    group.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Accept invalid TLS certificates (self-signed, intercepting proxies)",
    )
    group.add_argument("-G", "--no-headless", action="store_true", help="Show the browser window (needs a display)")
    group.add_argument("-B", "--binary", metavar="path", type=existing_file, help="Browser executable to use")
    group.add_argument(
        "-a", "--browser-arg", metavar="arg", action="append", default=[], help="Extra browser argument (repeatable)"
    )
    group.add_argument(
        "-P", "--pref", metavar="name=value", type=parse_pref, action="append", default=[],
        help="Firefox preference (repeatable)",
    )  # fmt: skip
    group.add_argument(
        "-t", "--timeout", metavar="seconds", type=positive_float, default=float(env("CRAU_TIMEOUT", "30")),
        help="Limit for starting, navigating and each wait (default: 30, or CRAU_TIMEOUT)",
    )  # fmt: skip
    return parent


def _page_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False, parents=[_browser_parent()])
    group = parent.add_argument_group("page")
    group.add_argument(
        "-U", "--wait-until", metavar="event", choices=WAIT_UNTIL_CHOICES, default="load",
        help=f"Navigation is done at this event. Options: {', '.join(WAIT_UNTIL_CHOICES)} (default: load)",
    )  # fmt: skip
    group.add_argument("-s", "--wait-selector", metavar="target", help="Then wait for this element to exist")
    group.add_argument("-j", "--wait-js", metavar="expr", help="Then wait for this JavaScript expression to be truthy")
    group.add_argument("-I", "--wait-idle", action="store_true", help="Then wait for the network to go quiet")
    group.add_argument("-r", "--scroll", action="store_true", help="Then scroll to the bottom (triggers lazy content)")
    group.add_argument("-w", "--wait", metavar="ms", type=int, default=0, help="Then wait this many milliseconds")
    group.add_argument(
        "-e", "--eval", metavar="js", action="append", default=[], help="Then run this JavaScript (repeatable)"
    )
    group.add_argument(
        "-E", "--eval-file", metavar="file", type=existing_file, help="Then run the JavaScript in this file"
    )
    group.add_argument("-C", "--console", action="store_true", help="Print the page's console messages to stderr")
    group.add_argument("-x", "--fail-on-http-error", action="store_true", help="Exit with code 8 on HTTP status >= 400")
    return parent


def add_commands(subparsers) -> None:
    """Register the browser commands on crau's top-level subparsers."""
    raw = argparse.RawDescriptionHelpFormatter
    page = _page_parent()
    _configure_browser(subparsers.add_parser("browser", help="Start, stop, inspect and install browsers"))
    _configure_tab(subparsers.add_parser("tab", help="Manage the tabs of a running browser"))
    _configure_page(
        subparsers.add_parser(
            "page", help="Get things from a page: HTML, text, JavaScript values, screenshots, PDF, cookies",
            epilog=EXIT_CODES, formatter_class=raw,
        ),  # fmt: skip
        page,
    )
    _configure_har(subparsers.add_parser("har", help="Capture network traffic to HAR, or summarize a HAR file"), page)

    from . import search

    search_parser = subparsers.add_parser(
        "search", help="Search the web through a real browser", description=search.__doc__, formatter_class=raw
    )
    search.configure_parser(search_parser)

    skill = subparsers.add_parser("skill", help="Print the agent skill (SKILL.md) that teaches an AI agent to use crau")
    skill.add_argument("-r", "--reference", metavar="name", help="Print this reference document instead")
    skill.add_argument("-l", "--list", action="store_true", help="List the reference documents")


def _configure_page(parser: argparse.ArgumentParser, page: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="action", required=True)

    sub = actions.add_parser("html", parents=[page], help="Print the rendered HTML of a page")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("-o", "--output", metavar="file", type=Path, help="Write here instead of stdout")

    sub = actions.add_parser("text", parents=[page], help="Print the visible text of a page")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("-o", "--output", metavar="file", type=Path, help="Write here instead of stdout")

    sub = actions.add_parser("js", parents=[page], help="Evaluate JavaScript in a page and print the result as JSON")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("expression", nargs="?", help="JavaScript expression (or use --file)")
    sub.add_argument("-f", "--file", metavar="file", type=existing_file, help="Read the JavaScript from this file")

    sub = actions.add_parser("outline", parents=[page], help="List headings, links, buttons and fields with selectors")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("-J", "--json", action="store_true", help="Print JSON instead of one line per element")

    sub = actions.add_parser("screenshot", parents=[page], help="Save a PNG or JPEG screenshot")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("output", type=Path, help="Image file to write (.png, .jpg or .jpeg)")
    sub.add_argument("-f", "--full-page", action="store_true", help="Capture the whole page, not only the viewport")
    sub.add_argument("-l", "--selector", metavar="target", help="Capture only this element")
    sub.add_argument("-Q", "--quality", metavar="n", type=int, choices=range(1, 101), help="JPEG quality, 1-100")

    sub = actions.add_parser("pdf", parents=[page], help="Print a page to PDF (chromium or firefox)")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("output", type=Path, help="PDF file to write")
    sub.add_argument("-L", "--landscape", action="store_true", help="Landscape orientation")
    sub.add_argument("-N", "--no-background", action="store_true", help="Do not print background colours and images")
    sub.add_argument("--header-template", metavar="html", help="Header HTML template (chromium only)")
    sub.add_argument("--footer-template", metavar="html", help="Footer HTML template (chromium only)")

    sub = actions.add_parser("cookies", parents=[page], help="Print the browser's cookies after loading a page")
    sub.add_argument("url", help=URL_HELP)
    sub.add_argument("-o", "--output", metavar="file", type=Path, help="Write here instead of stdout")
    sub.add_argument("-J", "--json", action="store_true", help="JSON instead of Netscape cookies.txt")


def _configure_har(parser: argparse.ArgumentParser, page: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="action", required=True)
    sub = actions.add_parser("capture", parents=[page], help="Visit pages and save every request and response")
    sub.add_argument("url", nargs="+", help="URL(s) to visit in order, in the same tab")
    sub.add_argument("output", type=Path, help="HAR file to write (nothing is filtered or redacted)")
    sub = actions.add_parser("summary", help="Print what a HAR contains: endpoints and the shape of bodies, no values")
    sub.add_argument("har", type=existing_file, help="HAR file")
    sub.add_argument("-u", "--url-filter", metavar="regex", help="Only entries whose URL matches")
    sub.add_argument("-m", "--method", metavar="method", help="Only entries with this HTTP method")
    sub.add_argument("-M", "--mime", metavar="text", help="Only entries whose response MIME type contains this")
    sub.add_argument("-J", "--json", action="store_true", help="Print JSON")


def _configure_browser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="action", required=True)
    sub = actions.add_parser("start", parents=[_browser_parent()], help="Launch a browser and leave it running")
    sub.add_argument("name", help="Name used later with --instance")
    sub = actions.add_parser("stop", parents=[_verbosity_parent()], help="Quit a running browser")
    sub.add_argument("name", help="Name given to 'crau browser start'")
    sub = actions.add_parser("status", help="List the running browsers: name, browser, pid and version")
    sub.add_argument("-J", "--json", action="store_true", help="Print JSON")
    sub = actions.add_parser("doctor", parents=[_verbosity_parent()], help="Show which browsers are usable and where")
    sub.add_argument("-J", "--json", action="store_true", help="Print JSON")

    from . import install

    install_parser = actions.add_parser(
        "install", help="Install chromium, firefox or lightpanda the right way for this distribution",
        description=install.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    install.configure_parser(install_parser)


def _configure_tab(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="action", required=True)
    sub = actions.add_parser("list", help="List open tabs: index, URL and title")
    sub.add_argument("instance", help="Name given to 'crau browser start'")
    sub.add_argument("-J", "--json", action="store_true", help="Print JSON")
    sub = actions.add_parser("open", help="Open a URL in a new tab and print its index")
    sub.add_argument("instance", help="Instance name")
    sub.add_argument("url", help="URL to open")
    sub = actions.add_parser("close", help="Close a tab")
    sub.add_argument("instance", help="Instance name")
    sub.add_argument("index", type=int, help="Tab index, as shown by 'tab list'")
