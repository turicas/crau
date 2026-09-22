from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CRAU = [sys.executable, "-m", "crau"]
EXPECTED = [
    {
        "position": 1,
        "title": "First result",
        "url": "https://example.org/one?a=1",
        "snippet": "Snippet of the first result.",
    },
    {"position": 2, "title": "Second result", "url": "https://example.com/two", "snippet": "Second snippet."},
]


def run(*arguments) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CRAU, "search", *map(str, arguments)], capture_output=True, text=True, timeout=120
    )


@pytest.mark.parametrize("engine, page", [("duckduckgo", "/ddg"), ("bing", "/bing")])
def test_results_page_is_read_through_the_browser(browser_name, site, engine, page):
    result = run("anything", "-e", engine, "-b", browser_name, "-J", "-u", site.url + page + "?q={query}")
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == EXPECTED
    assert ("GET", page + "?q=anything") in [(method, path) for method, path, _ in site.requests]


def test_text_output_limit_and_empty_results(browser_name, site):
    text = run("x y", "-b", browser_name, "-n", "1", "-u", site.url + "/ddg?q={query}")
    assert text.stdout == "1. First result\n   https://example.org/one?a=1\n   Snippet of the first result.\n"
    empty = run("x", "-b", browser_name, "-t", "3", "-u", site.url + "/final")
    assert empty.returncode == 1 and empty.stdout == ""
