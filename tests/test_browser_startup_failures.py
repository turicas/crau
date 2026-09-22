"""A browser that dies on startup must say why: its own output is the only place the reason is written."""

import stat

import pytest

from crau.browser.errors import LaunchError
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
async def test_startup_failure_reports_the_browser_output(fetcher_class, broken_browser, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    fetcher = fetcher_class(binary_path=str(broken_browser), timeout=5)
    with pytest.raises(LaunchError, match="exited with code 127") as error:
        async with fetcher:
            pass
    assert "libnss3.so" in str(error.value)
    # neither the log nor the temporary profile outlives the fetcher
    assert not list((tmp_path / "cache").rglob("browser.log"))
    assert not list((tmp_path / "cache" / "crau").glob("*-*"))
