import pytest

from crau.browser.errors import LaunchError
from crau.browser.launch import build_command
from crau.fetchers.chromium import ChromiumFetcher


@pytest.mark.asyncio
async def test_chromium_fetcher_missing_binary():
    with pytest.raises(LaunchError, match="nonexistent-chromium"):
        async with ChromiumFetcher(binary_path="nonexistent-chromium"):
            pass


def test_chromium_fetcher_passes_its_options_to_the_browser(tmp_path):
    custom_profile = tmp_path / "my-custom-profile"
    fetcher = ChromiumFetcher(
        binary_path="/usr/bin/chromium", user_data_dir=custom_profile, port=9222, window_size=(800, 600),
        extra_args=["--lang=pt-BR"], timeout=5,
    )  # fmt: skip
    options = fetcher._create_browser().options
    assert (options.browser, str(options.binary), options.profile) == ("chromium", "/usr/bin/chromium", custom_profile)
    assert (options.port, options.viewport, options.extra_args) == (9222, (800, 600), ["--lang=pt-BR"])
    assert options.timeout >= 30  # a cold start can take longer than the per-page timeout
    assert ChromiumFetcher().port is None  # no port is reserved beforehand: the browser picks one


def test_chromium_fetcher_launch_flags(tmp_path):
    options = ChromiumFetcher(user_data_dir=tmp_path / "p", port=9222)._create_browser().options
    args = build_command(options, "/usr/bin/chromium", tmp_path / "p", "Chromium 151.0.7922.137 built on Debian")
    assert "--headless=new" in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert "--enable-automation" not in args
    assert "--remote-debugging-port=9222" in args
    assert f"--user-data-dir={tmp_path / 'p'}" in args


def test_chromium_fetcher_hides_headless_token_with_a_launch_flag(tmp_path):
    """The User-Agent goes in as --user-agent, derived from --version, in the reduced form real Chrome sends.

    Overriding it through CDP without metadata empties navigator.userAgentData.brands and drops the
    sec-ch-ua header, which is a louder bot signal than the HeadlessChrome token it was hiding.
    """
    options = ChromiumFetcher()._create_browser().options
    args = build_command(options, "/usr/bin/chromium", tmp_path, "Chromium 151.0.7922.137 built on Debian GNU/Linux 13")
    flags = [arg for arg in args if arg.startswith("--user-agent=")]
    assert len(flags) == 1
    assert "Chrome/151.0.0.0 Safari/537.36" in flags[0] and "Headless" not in flags[0] and "7922" not in flags[0]

    custom = ChromiumFetcher(user_agent="custom/1")._create_browser().options
    assert "--user-agent=custom/1" in build_command(custom, "/usr/bin/chromium", tmp_path, "Chromium 151.0.7922.137")
