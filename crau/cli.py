from __future__ import annotations

import argparse
import datetime
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence
from urllib.parse import quote, urljoin, urlparse

from tqdm import tqdm
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from crau.cache.sqlite import SqliteCacheBackend
from crau.cache.warc import WarcCacheBackend
from crau.crawler import Crawler
from crau.io import archive_files
from crau.pipeline import ItemPipeline
from crau.utils import HTTP_STATUS_CODES, WarcReader, get_urls_from_file
from crau.version import __version__


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
        choices=["http", "lightpanda", "cdp"],
        default="http",
        help="Fetching backend engine (default: http)",
    )
    archive_parser.add_argument(
        "--format",
        choices=["warc", "har"],
        default="warc",
        help="Archive file format (default: warc)",
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
        choices=["sqlite", "warc", "none"],
        default="sqlite",
        help="Request cache backend (default: sqlite)",
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

    cache_backend = None
    if args.cache_warc:
        cache_backend = WarcCacheBackend(args.cache_warc)
    elif args.cache == "sqlite":
        cache_backend = SqliteCacheBackend()
    elif args.cache == "none":
        cache_backend = None

    pipeline = None
    if args.output_items:
        pipeline = ItemPipeline(args.output_items, default_format=args.items_format)

    warc_filename = args.output_filename if args.format == "warc" else None
    har_filename = args.output_filename if args.format == "har" else None

    crawler = Crawler(
        start_urls=urls,
        max_depth=args.max_depth,
        allowed_uris=args.allowed_uris,
        backend=args.backend,
        cache=cache_backend,
        cache_type=args.cache,
        warc_filename=warc_filename,
        har_filename=har_filename,
        pipeline=pipeline,
        concurrency=args.concurrency,
        concurrency_per_domain=args.concurrency_per_domain,
        autothrottle=args.autothrottle,
        user_agent=args.user_agent,
        timeout=args.timeout,
    )
    crawler.run()
    return 0


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    handlers = {
        "archive": handle_archive,
        "list": handle_list,
        "extract": handle_extract,
        "play": handle_play,
        "pack": handle_pack,
    }

    handler = handlers.get(args.command)
    if handler:
        return handler(args)
    return 1


def cli() -> None:
    """Entry point for console scripts."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
