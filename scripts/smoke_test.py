#!/usr/bin/env python3
import argparse
import sys

from crau.smoke import run_smoke_tests


def main() -> int:
    parser = argparse.ArgumentParser(description="Run end-to-end smoke tests for crau")
    parser.add_argument(
        "target_url",
        nargs="?",
        default="https://example.com",
        help="Target URL for smoke tests (default: https://example.com)",
    )
    args = parser.parse_args()

    success = run_smoke_tests(target_url=args.target_url)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
