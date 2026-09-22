"""Shared by the browser tests: skip (or fail, with REQUIRE_BROWSERS=1) when a browser is not installed."""

from __future__ import annotations

import os

import pytest

from crau.browser.errors import LaunchError
from crau.browser.launch import find_binary


def require_browser(name: str) -> None:
    try:
        find_binary(name)
    except LaunchError as exc:
        if os.environ.get("REQUIRE_BROWSERS") == "1":
            pytest.fail(f"REQUIRE_BROWSERS=1 but {exc}")
        pytest.skip(str(exc))
