from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from crau.browser.launch import group_is_running

ROOT = Path(__file__).resolve().parents[3]
CRAU = [sys.executable, "-m", "crau"]


def cli(*arguments, env=None, timeout=120) -> subprocess.CompletedProcess:
    command = [*CRAU, *map(str, arguments)]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})}, cwd=ROOT)


def test_html_text_and_js_print_data_to_stdout_and_nothing_else(browser_name, site):
    html = cli("page", "html", site.url + "/", "-b", browser_name, "-s", "#late")
    assert html.returncode == 0 and html.stderr == ""
    assert '<div id="late">hydrated</div>' in html.stdout
    text = cli("page", "text", site.url + "/final", "-b", browser_name)
    assert (text.returncode, text.stdout, text.stderr) == (0, "Final page\n", "")
    value = cli(
        "page", "js", site.url + "/form", "({title: document.title, fields: document.forms[0].length})", "-b", browser_name
    )
    assert value.returncode == 0 and json.loads(value.stdout)["title"] == "Form"


def test_eval_runs_before_the_capture_and_output_file_gets_parents(browser_name, site, tmp_path):
    output = tmp_path / "deep" / "page.html"
    result = cli("page", "html", site.url + "/final", "-b", browser_name, "-e", "document.title = 'changed'",
                 "-e", "document.body.dataset.x = document.title", "-o", output)  # fmt: skip
    assert (result.returncode, result.stdout) == (0, "")
    assert "<title>changed</title>" in output.read_text() and 'data-x="changed"' in output.read_text()


def test_verbose_and_console_go_to_stderr_only(full_browser_name, site):
    result = cli("page", "text", site.url + "/", "-b", full_browser_name, "-v", "-C", "-s", "#late")
    assert result.returncode == 0 and "Test site" in result.stdout
    assert "ready in" in result.stderr and "-> 200" in result.stderr and "index loaded" in result.stderr
    assert "ready in" not in result.stdout


def test_exit_codes_tell_failures_apart(browser_name, site, tmp_path):
    common = ("-b", browser_name, "-t", "3")
    assert cli("page", "html", "http://127.0.0.1:9/", *common).returncode == 4
    assert cli("page", "html", site.url + "/final", "-s", "#never", *common).returncode == 5
    failed_js = cli("page", "js", site.url + "/final", "notDefined.x", *common)
    assert failed_js.returncode == 6 and "notDefined" in failed_js.stderr and failed_js.stdout == ""
    assert cli("page", "text", site.url + "/missing", *common).returncode == 0
    strict = cli("page", "text", site.url + "/missing", "-x", *common)
    assert strict.returncode == 8 and "Not found" in strict.stdout and "404" in strict.stderr
    missing = cli(
        "page", "html", site.url, "-b", browser_name, env={f"CRAU_{browser_name.upper()}": "/no/such/binary"}
    )
    assert missing.returncode == 3
    if browser_name == "lightpanda":
        assert cli("page", "pdf", site.url + "/final", tmp_path / "x.pdf", *common).returncode == 7


def test_screenshot_pdf_cookies_and_headers(full_browser_name, site, tmp_path):
    shot = tmp_path / "shots" / "page.jpg"
    assert (
        cli("page", "screenshot", site.url + "/", shot, "-b", full_browser_name, "-f", "-Q", "70", "-V", "800x600").returncode
        == 0
    )
    assert shot.read_bytes()[:3] == b"\xff\xd8\xff"
    pdf = tmp_path / "page.pdf"
    assert cli("page", "pdf", site.url + "/final", pdf, "-b", full_browser_name, "-L").returncode == 0
    assert pdf.read_bytes()[:5] == b"%PDF-"
    headers = tmp_path / "headers.json"
    headers.write_text('{"X-From-File": "f"}')
    echoed = cli("page", "js", site.url + "/final", "fetch('/echo').then(r => r.json())", "-b", full_browser_name,
                 "-H", "X-Inline: i", "-F", headers)  # fmt: skip
    assert echoed.returncode == 0
    assert {"x-inline": "i", "x-from-file": "f"}.items() <= json.loads(echoed.stdout).items()
    jar = cli("page", "cookies", site.url + "/set-cookie", "-b", full_browser_name)
    assert jar.stdout.startswith("# Netscape HTTP Cookie File") and "\tsession\tabc123" in jar.stdout
    cookie_file = tmp_path / "jar.txt"
    cookie_file.write_text(jar.stdout)
    reused = cli("page", "js", site.url + "/final", "document.cookie", "-b", full_browser_name, "-c", cookie_file)
    assert json.loads(reused.stdout) == "session=abc123"


def test_har_capture_then_summary(browser_name, site, tmp_path):
    har_file = tmp_path / "out" / "site.har"
    captured = cli("har", "capture", site.url + "/", site.url + "/redirect", har_file, "-b", browser_name, "-I")
    assert (captured.returncode, captured.stdout, captured.stderr) == (0, "", "")
    entries = json.loads(har_file.read_text())["log"]["entries"]
    statuses = {entry["request"]["url"].replace(site.url, ""): entry["response"]["status"] for entry in entries}
    assert statuses["/"] == 200 and statuses["/redirect"] == 302 and statuses["/final"] == 200
    summary = cli("har", "summary", har_file, "-m", "POST")
    assert "POST 200 " + site.url + "/api/echo" in summary.stdout
    assert 'request body: {"field": "string", "other": "string"}' in summary.stdout
    assert "field=value" not in summary.stdout


def test_named_instance_workflow_across_separate_commands(full_browser_name, site, tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    name = f"cli-{full_browser_name}"
    assert cli("browser", "start", name, "-b", full_browser_name, env=env).returncode == 0
    try:
        listed = cli("browser", "status", env=env).stdout
        assert listed.startswith(f"{name}\t{full_browser_name}\t")
        index = cli("tab", "open", name, site.url + "/form", env=env).stdout.strip()
        assert index.isdigit()
        filled = cli("page", "js", f"tab:{index}", "document.querySelector('#user').value = 'kept'; 1", "-i", name, env=env)
        assert filled.returncode == 0
        kept = cli("page", "js", f"tab:{index}", "document.querySelector('#user').value", "-i", name, env=env)
        assert json.loads(kept.stdout) == "kept"
        tabs = json.loads(cli("tab", "list", name, "-J", env=env).stdout)
        assert tabs[int(index)]["url"] == site.url + "/form" and tabs[int(index)]["title"] == "Form"
        assert cli("tab", "close", name, index, env=env).returncode == 0
        assert all(
            tab["url"] != site.url + "/form" for tab in json.loads(cli("tab", "list", name, "-J", env=env).stdout)
        )
        assert cli("page", "html", "tab:99", "-i", name, env=env).returncode == 2
    finally:
        stopped = cli("browser", "stop", name, env=env)
    assert stopped.returncode == 0 and cli("browser", "status", env=env).stdout == ""
    assert cli("browser", "stop", name, env=env).returncode == 1


def browser_groups() -> set[int]:
    listing = subprocess.run(["ps", "-eo", "pgid,comm"], capture_output=True, text=True, timeout=30).stdout
    names = ("chromium", "chrome", "firefox", "firefox-bin", "lightpanda")
    return {int(line.split()[0]) for line in listing.splitlines()[1:] if line.split()[1] in names}


def default_sigint() -> None:
    """Shells start background jobs with SIGINT ignored and Python inherits that: make the test independent of it."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.mark.parametrize("signum, expected", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_interrupt_exits_with_signal_code_and_leaves_no_browser(full_browser_name, site, signum, expected):
    before = browser_groups()
    command = [*CRAU, "page", "html", site.url + "/final", "-b", full_browser_name, "-s", "#never"]
    command += ["-t", "90", "-v"]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=default_sigint,
    )
    try:
        for line in process.stderr:  # logged as soon as the browser is up; the command then loads the page and polls
            if "ready in" in line:
                break
        time.sleep(2)
        started = browser_groups() - before
        assert started, "the browser should be running while the command waits"
        process.send_signal(signum)
        assert process.wait(timeout=30) == expected
        time.sleep(0.5)
        assert not [group for group in started if group_is_running(group)]
    finally:
        if process.poll() is None:
            process.kill()
        for group in browser_groups() - before:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
