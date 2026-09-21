"""A browser that dies on startup must say why: its own output is the only place the reason is written."""

import stat

import pytest

from crau.fetchers.chromium import ChromiumFetcher
from crau.fetchers.firefox import FirefoxFetcher
from crau.fetchers.lightpanda import LightpandaFetcher


@pytest.fixture
def broken_browser(tmp_path):
    script = tmp_path / "broken-browser"
    script.write_text("#!/bin/sh\necho 'error while loading shared libraries: libnss3.so' >&2\nexit 127\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.asyncio
@pytest.mark.parametrize("fetcher_class", [ChromiumFetcher, FirefoxFetcher, LightpandaFetcher])
async def test_startup_failure_reports_the_browser_output(fetcher_class, broken_browser):
    fetcher = fetcher_class(binary_path=str(broken_browser), timeout=5)
    with pytest.raises(RuntimeError, match="exited prematurely with code 127") as error:
        async with fetcher:
            pass
    assert "libnss3.so" in str(error.value)
    assert fetcher._log_path is None  # the log file does not outlive the fetcher
