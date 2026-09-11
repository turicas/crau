import io
from pathlib import Path
from unittest.mock import patch
import pytest

from crau.cli import create_parser, main
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
            response=RawResponse(status_code=200, raw_body=b"World Content"),
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
