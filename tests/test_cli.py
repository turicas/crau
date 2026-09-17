import pytest

from crau.cli import main
from crau.version import __version__


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "archive" in captured.out
    assert "list" in captured.out
    assert "extract" in captured.out


def test_cli_archive_missing_urls(capsys):
    exit_code = main(["archive", "output.warc.gz"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one URL must be provided" in captured.err


def test_cli_list_and_extract(tmp_path, capsys):
    from warcio.warcwriter import WARCWriter
    from crau.models import NetworkTransaction, RawRequest, RawResponse

    warc_file = tmp_path / "test.warc.gz"
    with open(warc_file, "wb") as fobj:
        writer = WARCWriter(fobj, gzip=True)
        tx = NetworkTransaction(
            request=RawRequest(url="https://example.com/hello"),
            response=RawResponse(status_code=200, reason_phrase="OK", http_version="HTTP/1.1", raw_body=b"World Content"),
        )
        r1, r2 = tx.to_warc_records(writer)
        writer.write_record(r1)
        writer.write_record(r2)

    # Test list command
    exit_code = main(["list", str(warc_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "https://example.com/hello" in captured.out

    # Test extract to stdout (-)
    exit_code = main(["extract", str(warc_file), "https://example.com/hello", "-"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "World Content" in captured.out


DEFAULT_PAGES = {
    "/": ("text/html; charset=utf-8", b'<html><body><a href="/other">x</a><img src="/pic.png"></body></html>'),
    "/other": ("text/html; charset=utf-8", b"<html><body><h1>Other</h1></body></html>"),
    "/pic.png": ("image/png", b"\x89PNGfake"),
}


def _archived_uris(warc_file):
    from warcio.archiveiterator import ArchiveIterator

    with open(warc_file, "rb") as fobj:
        return {r.rec_headers.get_header("WARC-Target-URI") for r in ArchiveIterator(fobj) if r.rec_type == "response"}


def test_cli_archive_end_to_end(threaded_site, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    site = threaded_site(DEFAULT_PAGES)
    warc_file = tmp_path / "out.warc.gz"

    exit_code = main(["archive", str(warc_file), f"{site.url}/", "--allowed-uris", site.url, "--no-autothrottle"])

    assert exit_code == 0
    assert _archived_uris(warc_file) == {f"{site.url}/", f"{site.url}/other", f"{site.url}/pic.png"}
    # no cache file is created unless asked for
    assert not (tmp_path / ".crau_cache.sqlite").exists()


def test_cli_archive_sqlite_cache_is_actually_used(threaded_site, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    site = threaded_site(DEFAULT_PAGES)
    cache_path = tmp_path / "cache.sqlite"
    common = ["--max-depth", "0", "--no-autothrottle", "--cache", "sqlite", "--cache-path", str(cache_path)]

    assert main(["archive", str(tmp_path / "first.warc"), f"{site.url}/", *common]) == 0
    assert cache_path.exists()
    first_run_requests = len(site.requests)
    assert first_run_requests == 2  # page + image

    assert main(["archive", str(tmp_path / "second.warc"), f"{site.url}/", *common]) == 0
    assert len(site.requests) == first_run_requests, "second run should be served from cache"
    assert _archived_uris(tmp_path / "second.warc") == {f"{site.url}/", f"{site.url}/pic.png"}


def test_cli_archive_missing_cache_warc(tmp_path, capsys):
    exit_code = main(["archive", str(tmp_path / "o.warc"), "http://127.0.0.1:1/", "--cache-warc", str(tmp_path / "x.warc")])
    assert exit_code == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_archive_shows_progress_and_summary(threaded_site, tmp_path, capsys):
    site = threaded_site(DEFAULT_PAGES)
    exit_code = main(["archive", str(tmp_path / "o.warc"), f"{site.url}/", "--max-depth", "0", "--no-autothrottle"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Archiving" in err
    assert "Done: 2 fetched, 0 from cache, 0 failed." in err


def test_cli_archive_quiet_hides_progress_but_keeps_warnings(threaded_site, tmp_path, capsys, caplog):
    site = threaded_site(DEFAULT_PAGES)
    exit_code = main(
        ["-q", "archive", str(tmp_path / "o.warc"), f"{site.url}/", "http://127.0.0.1:1/", "--max-depth", "0",
         "--no-autothrottle", "--max-retries", "0"]
    )
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Archiving" not in err
    assert "Done: 2 fetched, 0 from cache, 1 failed." in err
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert any("Giving up on http://127.0.0.1:1/" in message for message in warnings)


def test_cli_archive_exit_code_when_nothing_archived(tmp_path, capsys):
    exit_code = main(["-q", "archive", str(tmp_path / "o.warc"), "http://127.0.0.1:1/", "--max-retries", "0"])
    assert exit_code == 1
    assert "1 failed" in capsys.readouterr().err


def test_cli_play_without_pywb_explains_how_to_install(tmp_path, capsys, monkeypatch):
    warc = tmp_path / "x.warc"
    warc.write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert main(["play", str(warc)]) == 2
    assert "crau[play]" in capsys.readouterr().err


def test_cli_archive_rejects_swapped_arguments(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["archive", "https://example.com/", "site.warc"]) == 2
    err = capsys.readouterr().err
    assert "Usage: crau archive OUTPUT_FILENAME URL" in err
    assert not (tmp_path / "https:").exists()

    assert main(["archive", "https://example.com/", "https://example.org/"]) == 2
    assert "first argument is the output file" in capsys.readouterr().err

    assert main(["archive", str(tmp_path / "o.warc"), "example.com/", "ftp://x/"]) == 2
    assert "must start with http://" in capsys.readouterr().err
