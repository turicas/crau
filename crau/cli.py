from __future__ import annotations

import argparse
import datetime
import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence
from urllib.parse import quote, urljoin, urlparse

from crau.version import __version__

ARCHIVE_COMMANDS = ("archive", "list", "extract", "play", "pack")


def load_archive_modules() -> ImportError | None:
    """Import what the archiving commands use into this module, the first time one of them runs.

    They need third-party packages (`pip install 'crau[archive]'`) and cost ~250 ms to import; the
    browser commands need neither, so `crau --help` and `crau page ...` must not pay for them.
    Returns the ImportError when a dependency is missing.
    """
    global tqdm, StatusAndHeaders, WARCWriter, SqliteCacheBackend, WarcCacheBackend, Crawler
    global archive_files, ItemPipeline, HTTP_STATUS_CODES, WarcReader, get_urls_from_file
    try:
        from tqdm import tqdm
        from warcio.statusandheaders import StatusAndHeaders
        from warcio.warcwriter import WARCWriter

        from crau.cache.sqlite import SqliteCacheBackend
        from crau.cache.warc import WarcCacheBackend
        from crau.crawler import Crawler
        from crau.io import archive_files
        from crau.pipeline import ItemPipeline
        from crau.utils import HTTP_STATUS_CODES, WarcReader, get_urls_from_file
    except ImportError as error:
        return error
    return None


def run_command(command: str) -> int:
    sys.stderr.write(f"*** Running command: {command}\n")
    return subprocess.call(shlex.split(command))


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crau",
        description="crau: High-fidelity web archiver and crawler",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not show progress; only log warnings and errors",
    )
    verbosity.add_argument(
        "--verbose",
        action="count",
        default=0,
        help="Log retries and cache hits (-v); repeat for debug output",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. archive
    archive_parser = subparsers.add_parser(
        "archive",
        help="Archive a list of URLs to a WARC or HAR file",
    )
    archive_parser.add_argument(
        "output_filename",
        type=Path,
        help="Destination WARC or HAR filename",
    )
    archive_parser.add_argument(
        "urls",
        nargs="*",
        help="List of URLs to archive",
    )
    archive_parser.add_argument(
        "-i",
        "--input-filename",
        type=Path,
        help="Path to text file containing one URL per line",
    )
    archive_parser.add_argument(
        "--input-encoding",
        default="utf-8",
        help="Encoding of the input text file (default: utf-8)",
    )
    archive_parser.add_argument(
        "--max-depth",
        type=int,
        default=1,
        help="Maximum crawl depth (default: 1)",
    )
    archive_parser.add_argument(
        "--allowed-uris",
        action="append",
        default=[],
        help="Restrict crawling to specific domain or URI prefix (repeatable)",
    )
    archive_parser.add_argument(
        "--backend",
        choices=["http", "chromium", "firefox", "lightpanda", "cdp"],
        default="http",
        help="Fetching backend engine (default: http)",
    )
    archive_parser.add_argument(
        "--binary-path",
        type=Path,
        help="Path to custom browser executable binary (Chromium, Firefox, Lightpanda)",
    )
    archive_parser.add_argument(
        "--browser-settle",
        type=float,
        default=10.0,
        help="Browser backends: seconds to keep capturing after the load event, scrolling the page and "
        "waiting for the network to go idle, so XHR, lazy images and infinite scroll are archived "
        "(default: 10; 0 collects right after load)",
    )
    archive_parser.add_argument(
        "--no-auto-scroll",
        action="store_true",
        help="Browser backends: do not scroll the page during the settle phase",
    )
    archive_parser.add_argument(
        "--user-data-dir",
        type=Path,
        help="Custom user data directory / profile for browser backends",
    )
    archive_parser.add_argument(
        "--format",
        choices=["warc", "har", "rendered-har", "rendered-only-har"],
        default="warc",
        help="Archive file format: warc, har (wire-level), rendered-har (all network + target DOM) or rendered-only-har (only target DOMs) (default: warc)",
    )
    archive_parser.add_argument(
        "--concurrency",
        type=int,
        default=16,
        help="Maximum concurrent requests (default: 16)",
    )
    archive_parser.add_argument(
        "--concurrency-per-domain",
        type=int,
        default=4,
        help="Maximum concurrent requests per domain (default: 4)",
    )
    archive_parser.add_argument(
        "--autothrottle",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable adaptive latency-based throttling (default: enabled)",
    )
    archive_parser.add_argument(
        "--cache",
        choices=["sqlite", "none"],
        default="none",
        help="Reuse responses fetched in a previous run instead of hitting the network again (default: none)",
    )
    archive_parser.add_argument(
        "--cache-path",
        type=Path,
        default=Path(".crau_cache.sqlite"),
        help="SQLite file used by --cache sqlite (default: .crau_cache.sqlite)",
    )
    archive_parser.add_argument(
        "--cache-warc",
        type=Path,
        help="Path to existing WARC file to use as read-only cache",
    )
    archive_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Network request timeout in seconds (default: 15.0)",
    )
    archive_parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retries per URL on network errors, with exponential backoff (default: 3)",
    )
    archive_parser.add_argument(
        "--user-agent",
        help="Custom User-Agent header",
    )
    archive_parser.add_argument(
        "--output-items",
        type=Path,
        help="Output directory for scraped items (JSONL/CSV)",
    )
    archive_parser.add_argument(
        "--items-format",
        choices=["jsonl", "csv"],
        default="jsonl",
        help="Export format for scraped dicts (default: jsonl)",
    )

    # 2. list
    list_parser = subparsers.add_parser(
        "list",
        help="List URIs of response records stored in a WARC file",
    )
    list_parser.add_argument(
        "warc_filename",
        type=Path,
        help="Path to the WARC file",
    )

    # 3. extract
    extract_parser = subparsers.add_parser(
        "extract",
        help="Extract URL content from archive",
    )
    extract_parser.add_argument(
        "warc_filename",
        type=Path,
        help="Path to the WARC file",
    )
    extract_parser.add_argument(
        "uri",
        help="URI to extract from the archive",
    )
    extract_parser.add_argument(
        "output",
        help="Destination output file path (or '-' for stdout)",
    )
    extract_parser.add_argument(
        "--chunk-size",
        type=int,
        default=512 * 1024,
        help="Chunk buffer size in bytes",
    )

    # 4. play
    play_parser = subparsers.add_parser(
        "play",
        help="Run a local web server playing your archive",
    )
    play_parser.add_argument(
        "warc_filename",
        type=Path,
        help="Path to the WARC file",
    )
    play_parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8000,
        help="Server port (default: 8000)",
    )
    play_parser.add_argument(
        "-b",
        "--bind",
        default="127.0.0.1",
        help="Server host bind address (default: 127.0.0.1)",
    )

    # 5. pack
    pack_parser = subparsers.add_parser(
        "pack",
        help="Pack one or more local files into a WARC",
    )
    pack_parser.add_argument(
        "start_url",
        help="Base URL for the files",
    )
    pack_parser.add_argument(
        "path_or_archive",
        type=Path,
        help="Local directory or archive (.tar.gz, .zip) to pack",
    )
    pack_parser.add_argument(
        "warc_filename",
        type=Path,
        help="Destination WARC filename",
    )
    pack_parser.add_argument(
        "--inner-directory",
        type=Path,
        help="Inner directory inside archive to retrieve files from",
    )

    # 6. browser automation: browser, tab, page, har and search (standard library only)
    from crau.browser.cli import add_commands

    add_commands(subparsers)

    return parser


def handle_archive(args: argparse.Namespace) -> int:
    urls = list(args.urls)
    if args.input_filename:
        if not args.input_filename.exists():
            sys.stderr.write(f"ERROR: filename {args.input_filename} does not exist.\n")
            return 2
        urls.extend(get_urls_from_file(str(args.input_filename), encoding=args.input_encoding))

    if not urls:
        sys.stderr.write(
            "ERROR: at least one URL must be provided (or a file containing one per line via -i).\n"
        )
        return 2

    # The output file comes first; catch the common mistake of passing a URL there
    if re.match(r"^[a-z][a-z0-9+.-]*:/", str(args.output_filename), re.IGNORECASE):  # Path() collapses "://"
        sys.stderr.write(
            f"ERROR: the first argument is the output file, got a URL: {args.output_filename}\n"
            "Usage: crau archive OUTPUT_FILENAME URL [URL ...]\n"
        )
        return 2
    bad_urls = [url for url in urls if not url.lower().startswith(("http://", "https://"))]
    if bad_urls:
        sys.stderr.write(
            f"ERROR: URLs must start with http:// or https://: {', '.join(bad_urls[:5])}\n"
            "Usage: crau archive OUTPUT_FILENAME URL [URL ...]\n"
        )
        return 2

    cache_backend = None
    if args.cache_warc:
        if not args.cache_warc.exists():
            sys.stderr.write(f"ERROR: cache WARC {args.cache_warc} does not exist.\n")
            return 2
        cache_backend = WarcCacheBackend(args.cache_warc)
    elif args.cache == "sqlite":
        cache_backend = SqliteCacheBackend(args.cache_path)

    pipeline = None
    if args.output_items:
        pipeline = ItemPipeline(args.output_items, default_format=args.items_format)

    is_har = args.format in ("har", "rendered-har", "rendered-only-har")
    warc_filename = None if is_har else args.output_filename
    har_filename = args.output_filename if is_har else None

    progress_bar = None
    progress = None
    if not args.quiet:
        progress_bar = tqdm(desc="Archiving", unit="req", dynamic_ncols=True, file=sys.stderr)

        def progress(stats: dict[str, int]) -> None:
            progress_bar.n = stats["fetched"] + stats["cached"] + stats["failed"]
            progress_bar.total = progress_bar.n + stats["pending"]
            progress_bar.set_postfix(failed=stats["failed"], cached=stats["cached"], refresh=False)
            progress_bar.refresh()

    crawler = Crawler(
        start_urls=urls,
        max_depth=args.max_depth,
        allowed_uris=args.allowed_uris,
        backend=args.backend,
        cache=cache_backend,
        cache_type=args.cache,
        warc_filename=warc_filename,
        har_filename=har_filename,
        format=args.format,
        pipeline=pipeline,
        concurrency=args.concurrency,
        concurrency_per_domain=args.concurrency_per_domain,
        autothrottle=args.autothrottle,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_retries=args.max_retries,
        binary_path=args.binary_path,
        user_data_dir=args.user_data_dir,
        browser_settle=args.browser_settle,
        browser_auto_scroll=not args.no_auto_scroll,
        progress=progress,
    )
    try:
        crawler.run()
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted; partial output kept.\n")
        return 130
    finally:
        if progress_bar is not None:
            progress_bar.close()
    stats = crawler.stats
    sys.stderr.write(f"Done: {stats['fetched']} fetched, {stats['cached']} from cache, {stats['failed']} failed.\n")
    return 1 if stats["failed"] and not (stats["fetched"] or stats["cached"]) else 0


def handle_list(args: argparse.Namespace) -> int:
    if not args.warc_filename.exists():
        sys.stderr.write(f"ERROR: filename {args.warc_filename} does not exist.\n")
        return 2

    warc = WarcReader(str(args.warc_filename))
    for record in warc:
        if record.rec_type == "response":
            uri = record.rec_headers.get_header("WARC-Target-URI")
            if uri:
                sys.stdout.write(f"{uri}\n")
    return 0


def handle_extract(args: argparse.Namespace) -> int:
    if not args.warc_filename.exists():
        sys.stderr.write(f"ERROR: filename {args.warc_filename} does not exist.\n")
        return 2

    warc = WarcReader(str(args.warc_filename))
    response = warc.get_response(args.uri)
    if response is None:
        sys.stderr.write(f"ERROR: URI {args.uri} not found in archive.\n")
        return 1

    stream = response.content_stream()
    chunk_size = args.chunk_size

    if args.output == "-":
        data = stream.read(chunk_size)
        while data != b"":
            sys.stdout.buffer.write(data)
            data = stream.read(chunk_size)
    else:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, mode="wb") as fobj:
            data = stream.read(chunk_size)
            while data != b"":
                fobj.write(data)
                data = stream.read(chunk_size)
    return 0


def handle_play(args: argparse.Namespace) -> int:
    filename = args.warc_filename
    if not filename.exists():
        sys.stderr.write(f"ERROR: filename {filename} does not exist.\n")
        return 2

    if shutil.which("wayback") is None or shutil.which("wb-manager") is None:
        sys.stderr.write(
            "ERROR: `crau play` needs pywb (the `wayback` and `wb-manager` commands). "
            "Install it with `pip install crau[play]` (pywb supports Python 3.7-3.12 only).\n"
        )
        return 2

    full_filename = filename.resolve()
    collection_name = filename.name.split(".")[0]
    temp_dir = tempfile.mkdtemp()
    old_cwd = os.getcwd()

    try:
        os.chdir(temp_dir)
        run_command(f'wb-manager init "{collection_name}"')
        run_command(f'wb-manager add "{collection_name}" "{full_filename}"')
        run_command(f"wayback -p {args.port} -b {args.bind}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.chdir(old_cwd)
    return 0


def handle_pack(args: argparse.Namespace) -> int:
    start_url = args.start_url
    if not start_url.endswith("/"):
        start_url = start_url + "/"

    path_or_archive = args.path_or_archive
    warc_filename = args.warc_filename
    warc_filename.parent.mkdir(parents=True, exist_ok=True)

    offset = time.timezone if (time.localtime().tm_isdst == 0) else time.altzone
    tz = datetime.timezone(offset=-datetime.timedelta(seconds=offset))
    with open(warc_filename, mode="wb") as warc_fobj:
        writer = WARCWriter(warc_fobj, gzip=warc_filename.suffixes[-1].lower() == ".gz")
        for file_info in tqdm(
            archive_files(path_or_archive, args.inner_directory),
            "Packing files",
            file=sys.stderr,
        ):
            if file_info.is_dir:
                continue
            warc_headers_dict = {
                "WARC-Date": file_info.created_at.replace(tzinfo=tz).strftime(
                    "%Y-%m-%dT%H:%M:%S%z"
                ),
            }
            url = urljoin(start_url, str(file_info.path))
            path = url[url.find("/", len(urlparse(url).scheme) + 3) :]
            url = url[: len(url) - len(path)] + quote(path)
            http_headers = StatusAndHeaders(
                f"GET {quote(path)} HTTP/1.1", [], is_http_request=True
            )
            writer.write_record(
                writer.create_warc_record(
                    url,
                    "request",
                    http_headers=http_headers,
                    warc_headers_dict=warc_headers_dict,
                )
            )

            status_code = 200
            header_list = [("Content-Length", str(file_info.size))]
            content_type, _ = mimetypes.guess_type(path)
            if content_type is not None:
                header_list.append(("Content-Type", content_type))
            status_title = HTTP_STATUS_CODES.get(status_code, "Unknown")
            http_headers = StatusAndHeaders(
                f"{status_code} {status_title}",
                header_list,
                protocol="HTTP/1.1",
                is_http_request=False,
            )
            writer.write_record(
                writer.create_warc_record(
                    url,
                    "response",
                    payload=file_info.fobj,
                    http_headers=http_headers,
                    warc_headers_dict=warc_headers_dict,
                )
            )
    return 0


def configure_logging(quiet: bool, verbose: int) -> None:
    level = logging.WARNING
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if quiet:
        logging.getLogger("crau").setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    configure_logging(args.quiet, args.verbose)

    handlers = {
        "archive": handle_archive,
        "list": handle_list,
        "extract": handle_extract,
        "play": handle_play,
        "pack": handle_pack,
    }

    from crau.browser.cli import COMMANDS as BROWSER_COMMANDS

    if args.command in BROWSER_COMMANDS:
        from crau.browser.commands import run as run_browser_command

        return run_browser_command(args)

    if args.command in ARCHIVE_COMMANDS:
        missing = load_archive_modules()
        if missing is not None:
            sys.stderr.write(
                f"crau: error: '{args.command}' needs the archiving dependencies ({missing}). "
                "Install them with: pip install 'crau[archive]'\n"
            )
            return 1

    handler = handlers.get(args.command)
    if handler:
        return handler(args)
    return 1


def cli() -> None:
    """Entry point for console scripts."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
