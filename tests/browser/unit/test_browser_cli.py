from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crau.browser.cli import parse_header_argument, parse_pref, parse_viewport
from crau.cli import create_parser, main

CRAU = [sys.executable, "-m", "crau"]


def run_main(argv, capsys) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as exit_request:  # argparse reports usage errors this way
        code = exit_request.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_is_fast_and_lists_the_browser_commands():
    started = time.perf_counter()
    result = subprocess.run([*CRAU, "--help"], capture_output=True, text=True, timeout=30, check=True)
    assert time.perf_counter() - started < 0.5  # the CLI guide asks for 100 ms; the margin absorbs slow CI machines
    assert result.stderr == ""
    for command in ("archive", "browser", "tab", "page", "har", "search", "skill"):
        assert command in result.stdout


def test_page_help_documents_exit_codes():
    result = subprocess.run([*CRAU, "page", "--help"], capture_output=True, text=True, timeout=30, check=True)
    assert "exit codes:" in result.stdout and "130" in result.stdout
    for action in ("html", "text", "js", "outline", "screenshot", "pdf", "cookies"):
        assert action in result.stdout


def test_help_imports_neither_asyncio_nor_the_archiving_dependencies():
    code = (
        "import sys\n"
        "from crau.cli import create_parser\n"
        "create_parser()\n"
        "print([name for name in ('asyncio', 'crau.browser.session', 'warcio', 'lxml', 'tqdm') if name in sys.modules])\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "[]", result.stderr


def test_command_is_required_and_unknown_options_are_rejected(capsys):
    assert run_main([], capsys)[0] == 2
    code, _, err = run_main(["page", "html", "http://x", "--bogus"], capsys)
    assert code == 2 and "--bogus" in err
    assert run_main(["page"], capsys)[0] == 2
    assert run_main(["browser"], capsys)[0] == 2


def test_custom_argument_types():
    assert parse_viewport("1280X800") == (1280, 800)
    assert parse_header_argument("X-Key:  a:b ") == ("X-Key", "a:b")
    assert parse_pref("a.b=true") == ("a.b", True) and parse_pref("n=-3") == ("n", -3)
    assert parse_pref("s=x y") == ("s", "x y")
    bad_values = ((parse_viewport, "1280"), (parse_viewport, "0x10"), (parse_header_argument, "nocolon"), (parse_pref, "=1"))
    for function, bad in bad_values:
        with pytest.raises(Exception, match="Invalid"):
            function(bad)


def test_page_options_end_up_typed(tmp_path):
    cookie_file = tmp_path / "c.txt"
    cookie_file.write_text("")
    args = create_parser().parse_args(
        ["page", "screenshot", "http://x", "out/shot.png", "-f", "-b", "firefox", "-V", "800x600", "-H", "A: 1",
         "-H", "B: 2", "-c", str(cookie_file), "-P", "x=1", "-w", "250", "-e", "a()", "-e", "b()", "-t", "5", "-S", "-k"]
    )  # fmt: skip
    assert (args.command, args.action) == ("page", "screenshot")
    assert (args.browser, args.viewport, args.header) == ("firefox", (800, 600), [("A", "1"), ("B", "2")])
    assert (args.output, args.cookies, args.wait, args.eval, args.timeout, args.pref) == (
        Path("out/shot.png"), cookie_file, 250, ["a()", "b()"], 5.0, [("x", 1)],
    )  # fmt: skip
    assert args.full_page and args.no_stealth and args.insecure and not args.no_headless


def test_quiet_and_verbose_are_mutually_exclusive_and_bad_choices_fail(capsys):
    assert run_main(["page", "html", "http://x", "-q", "-v"], capsys)[0] == 2
    code, _, err = run_main(["page", "html", "http://x", "-b", "netscape"], capsys)
    assert code == 2 and "netscape" in err
    assert run_main(["page", "html", "http://x", "-c", "/no/such/file"], capsys)[0] == 2


def test_command_verbosity_does_not_clobber_the_global_flags():
    args = create_parser().parse_args(["-q", "page", "html", "http://x", "-v"])
    assert args.quiet is True and args.browser_verbose is True and args.browser_quiet is False


def test_environment_variables_provide_defaults(monkeypatch):
    monkeypatch.setenv("CRAU_BROWSER", "lightpanda")
    monkeypatch.setenv("CRAU_TIMEOUT", "7")
    args = create_parser().parse_args(["page", "text", "http://x"])
    assert (args.browser, args.timeout) == ("lightpanda", 7.0)
    assert create_parser().parse_args(["page", "text", "http://x", "-b", "firefox"]).browser == "firefox"


def test_usage_problems_found_after_parsing_exit_with_2_without_starting_a_browser(capsys, tmp_path):
    code, _, err = run_main(["page", "js", "http://x"], capsys)
    assert code == 2 and "expression or --file" in err
    code, _, err = run_main(["page", "html", "tab:0"], capsys)
    assert code == 2 and "--instance" in err
    code, _, err = run_main(["page", "screenshot", "http://x", str(tmp_path / "shot.gif")], capsys)
    assert code == 2 and ".gif" in err


def test_har_summary_command_prints_shapes_as_text_and_json(capsys, tmp_path):
    content = {"mimeType": "application/json", "size": 9, "text": '{"id": 1}'}
    entry = {
        "request": {"method": "GET", "url": "https://x.org/api?page=1", "headers": [{"name": "Accept", "value": "*/*"}],
                    "queryString": [{"name": "page", "value": "1"}]},
        "response": {"status": 200, "content": content},
    }  # fmt: skip
    har = tmp_path / "x.har"
    har.write_text(json.dumps({"log": {"entries": [entry]}}))
    code, out, err = run_main(["har", "summary", str(har)], capsys)
    assert code == 0 and err == ""
    assert "[0] GET 200 https://x.org/api" in out and 'response body: {"id": "number"}' in out and "query: page" in out
    code, out, _ = run_main(["har", "summary", str(har), "-J", "-m", "GET"], capsys)
    assert json.loads(out)[0]["response_body"] == {"id": "number"}
    assert run_main(["har", "summary", str(har), "-m", "POST"], capsys)[1].strip() == ""


def test_unknown_instance_is_a_plain_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    code, out, err = run_main(["tab", "list", "nope"], capsys)
    assert (code, out) == (1, "") and "No instance named 'nope'" in err
    code, out, _ = run_main(["browser", "status"], capsys)
    assert (code, out) == (0, "")


def test_install_and_search_command_line_validation(capsys):
    for argv in (["browser", "install"], ["browser", "install", "netscape"], ["browser", "install", "chromium", "-m", "x"],
                 ["browser", "install", "chromium", "-q", "-v"], ["search"], ["search", "q", "-e", "altavista"],
                 ["search", "q", "-n", "0"], ["search", "q", "-b", "netscape"]):  # fmt: skip
        assert run_main(argv, capsys)[0] == 2, argv


def test_install_dry_run_prints_the_plan_and_changes_nothing(capsys):
    code, out, _ = run_main(["browser", "install", "lightpanda", "firefox", "--dry-run", "--channel", "esr"], capsys)
    assert code == 0 and "# lightpanda" in out and "# firefox" in out and "latest/download" in out
