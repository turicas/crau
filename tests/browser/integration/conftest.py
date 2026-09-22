"""Fixtures for tests that drive real browsers against the local test site."""

from __future__ import annotations

import pytest
from browsers import require_browser
from testsite import Site

from crau.browser.launch import BROWSERS


@pytest.fixture(params=BROWSERS)
def browser_name(request) -> str:
    require_browser(request.param)
    return request.param


@pytest.fixture(params=["chromium", "firefox"])
def full_browser_name(request) -> str:
    """Browsers with a real rendering engine, tabs and persistent state (everything except Lightpanda)."""
    require_browser(request.param)
    return request.param


@pytest.fixture
def site():
    with Site() as running:
        yield running
