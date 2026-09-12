#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crau.smoke import is_browser_available, run_smoke_tests


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run end-to-end smoke tests for crau with known sites (e.g. Wikipedia)"
    )
    parser.add_argument(
        "--target-url",
        default="https://en.wikipedia.org/wiki/Main_Page",
        help="Target URL for tests (default: https://en.wikipedia.org/wiki/Main_Page)",
    )
    parser.add_argument(
        "--backend",
        choices=["http", "chromium", "firefox", "lightpanda", "all"],
        default="http",
        help="Backend engine to test (default: http)",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        help="Custom profile directory for browser backends",
    )
    parser.add_argument(
        "--binary-path",
        type=Path,
        help="Custom executable path for browser backends",
    )
    args = parser.parse_args()

    backends = (
        ["http", "chromium", "firefox", "lightpanda"]
        if args.backend == "all"
        else [args.backend]
    )

    failures = 0
    tested = 0
    for b in backends:
        if not is_browser_available(b):
            print(f"[SKIP] Backend '{b}' not installed on this system.")
            continue
        tested += 1
        ok = run_smoke_tests(
            target_url=args.target_url,
            backend=b,
            user_data_dir=args.user_data_dir,
            binary_path=args.binary_path,
        )
        if not ok:
            failures += 1

    if tested == 0:
        print("No requested backends were available to run.")
        return 1

    return 1 if failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
