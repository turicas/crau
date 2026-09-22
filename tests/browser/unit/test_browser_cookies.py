from __future__ import annotations

import json

import pytest
from crau.browser.cookies import dump_netscape, load_cookies, load_headers, parse_header, parse_netscape

NETSCAPE = (
    "# Netscape HTTP Cookie File\n\n"
    ".example.com\tTRUE\t/\tTRUE\t1893456000\tsession\tabc=123\n"
    "#HttpOnly_app.example.com\tFALSE\t/admin\tFALSE\t0\ttoken\txyz\n"
)


def test_netscape_file_with_http_only_prefix_session_cookie_and_equals_in_value(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE)
    first, second = load_cookies(path)
    assert first == {
        "name": "session", "value": "abc=123", "domain": ".example.com", "path": "/", "secure": True,
        "http_only": False, "expires": 1893456000.0, "same_site": None,
    }  # fmt: skip
    assert (second["domain"], second["path"], second["http_only"], second["expires"]) == (
        "app.example.com", "/admin", True, None,
    )  # fmt: skip


def test_netscape_round_trip():
    assert parse_netscape(dump_netscape(parse_netscape(NETSCAPE))) == parse_netscape(NETSCAPE)


def test_malformed_netscape_line_reports_its_number():
    with pytest.raises(ValueError, match="line 2"):
        parse_netscape("# comment\nonly\tthree\tfields\n")


def test_json_list_and_storage_state_with_browser_style_keys(tmp_path):
    cookie = {"name": "a", "value": 1, "domain": "x.org", "httpOnly": True, "sameSite": "Lax", "expirationDate": 99.5}
    for payload in ([cookie], {"cookies": [cookie], "origins": []}):
        path = tmp_path / "cookies.json"
        path.write_text(json.dumps(payload))
        (loaded,) = load_cookies(path)
        assert (loaded["value"], loaded["http_only"], loaded["same_site"], loaded["expires"]) == (
            "1",
            True,
            "Lax",
            99.5,
        )


def test_cookie_needs_name_value_and_a_domain_or_url(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([{"name": "a", "value": "b"}]))
    with pytest.raises(ValueError, match="domain or a url"):
        load_cookies(path)
    path.write_text(json.dumps([{"name": "a", "value": "b", "url": "https://x.org/"}]))
    assert load_cookies(path)[0]["url"] == "https://x.org/"


def test_header_parsing_and_header_files(tmp_path):
    assert parse_header("Authorization: Bearer a:b") == ("Authorization", "Bearer a:b")
    for bad in ("no separator", ": empty name", "Bad Name: x"):
        with pytest.raises(ValueError):
            parse_header(bad)
    lines = tmp_path / "headers.txt"
    lines.write_text("# comment\n\nX-One: 1\nAccept-Language: pt-BR, en;q=0.5\n")
    assert load_headers(lines) == {"X-One": "1", "Accept-Language": "pt-BR, en;q=0.5"}
    as_json = tmp_path / "headers.json"
    as_json.write_text('{"X-Two": 2}')
    assert load_headers(as_json) == {"X-Two": "2"}
