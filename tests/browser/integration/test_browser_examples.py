"""The examples are documentation that must keep working: every one is executed against the test site."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXPECTED_FIELDS = ["User name", "Password", "Search term", "Colour", "Remember me"]


def run(*command, env=None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(part) for part in command], capture_output=True, text=True, timeout=180, env={**os.environ, **(env or {})}, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    return result


def python_example(name: str, *arguments, env=None) -> subprocess.CompletedProcess:
    return run(sys.executable, ROOT / "examples" / name, *arguments, env=env)


def check_form_flow(output: str, directory: Path) -> None:
    result = json.loads(output)
    assert result["fields"] == EXPECTED_FIELDS and result["title"] == "signed-in"
    assert (
        result["received"]["user"] == "alice" and result["received"]["color"] == "g" and result["received"]["remember"]
    )
    assert (directory / "signed-in.png").read_bytes()[:4] == b"\x89PNG"


def test_form_flow_python(browser_name, site, tmp_path):
    check_form_flow(python_example("browser_form_flow.py", site.url, tmp_path, "-b", browser_name).stdout, tmp_path)


def test_multi_tab(browser_name, site):
    urls = [site.url + "/", site.url + "/final", "http://127.0.0.1:9/"]
    rows = [json.loads(line) for line in python_example("browser_multi_tab.py", *urls, "-b", browser_name).stdout.splitlines()]
    assert [row["url"] for row in rows] == urls
    assert (rows[0]["title"], rows[0]["status"], rows[0]["links"]) == ("Test site", 200, 2)
    assert rows[1]["title"] == "Final" and "error" in rows[2]


def test_capture_har(browser_name, site, tmp_path):
    har_file = tmp_path / "out" / "site.har"
    rows = json.loads(python_example("browser_capture_har.py", site.url, har_file, "-b", browser_name).stdout)
    login = [row for row in rows if row["method"] == "POST" and row["endpoint"].endswith("/api/echo")]
    assert login and login[0]["request_body"]["user"] == "string"
    assert login[0]["response_body"]["received"]["user"] == "string"
    assert len(json.loads(har_file.read_text())["log"]["entries"]) >= 2


def test_reuse_instance(full_browser_name, site, tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    cli = [sys.executable, "-m", "crau"]
    run(*cli, "browser", "start", "example", "-b", full_browser_name, env=env)
    try:
        first = json.loads(python_example("browser_reuse_instance.py", "example", site.url + "/form", env=env).stdout)
        second = json.loads(python_example("browser_reuse_instance.py", "example", site.url + "/form", env=env).stdout)
    finally:
        run(*cli, "browser", "stop", "example", env=env)
    assert (first["reused"], first["visits"]) == (False, 1)
    assert (second["reused"], second["visits"], second["tabs"]) == (True, 2, first["tabs"])
