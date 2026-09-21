from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from crau.fetchers.chromium import ChromiumFetcher


def test_chromium_fetcher_missing_binary():
    with patch("shutil.which", return_value=None):
        fetcher = ChromiumFetcher(binary_path="nonexistent-chromium")
        with pytest.raises(FileNotFoundError, match="Chromium binary not found"):
            fetcher._resolve_binary()


@pytest.mark.asyncio
async def test_chromium_fetcher_custom_user_data_dir(tmp_path):
    custom_profile = tmp_path / "my-custom-profile"

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()

    async def fake_wait():
        return 0

    fake_proc.wait = fake_wait

    with patch("shutil.which", return_value="/usr/bin/chromium"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            with patch.object(ChromiumFetcher, "_wait_until_ready", new_callable=AsyncMock):
                with patch.object(ChromiumFetcher, "_connect_cdp", new_callable=AsyncMock):
                    fetcher = ChromiumFetcher(user_data_dir=custom_profile, port=9222)
                    async with fetcher:
                        assert fetcher.user_data_dir == custom_profile
                        assert not fetcher._is_temp_profile

                    # Ensure the profile was passed in command
                    cmd_args = mock_exec.call_args[0]
                    assert any(f"--user-data-dir={custom_profile}" in arg for arg in cmd_args)
                    fake_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_chromium_fetcher_strips_headless_token_from_user_agent():
    """Debian Chromium advertises HeadlessChrome/N; use the same UA without the token unless one was given."""
    native = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36"
    with patch.object(ChromiumFetcher, "_wait_until_ready", new=AsyncMock(return_value=("ws://x/devtools/browser/1", native))):
        with patch("crau.fetchers.cdp.CdpBrowserFetcher.__aenter__", new_callable=AsyncMock):
            fetcher = ChromiumFetcher(port=9222)
            await fetcher._connect_cdp()
            assert fetcher.endpoint_url == "ws://x/devtools/browser/1"
            assert "HeadlessChrome" not in fetcher.user_agent
            assert "Chrome/153.0.0.0" in fetcher.user_agent

            fetcher = ChromiumFetcher(port=9222, user_agent="custom/1")
            await fetcher._connect_cdp()
            assert fetcher.user_agent == "custom/1"


@pytest.mark.asyncio
async def test_chromium_fetcher_launch_flags(tmp_path):
    fake_proc = MagicMock()
    fake_proc.returncode = None

    async def fake_wait():
        return 0

    fake_proc.wait = fake_wait
    with patch("shutil.which", return_value="/usr/bin/chromium"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            with patch.object(ChromiumFetcher, "_connect_cdp", new_callable=AsyncMock):
                async with ChromiumFetcher(user_data_dir=tmp_path / "p", port=9222):
                    pass
    args = mock_exec.call_args[0]
    env = mock_exec.call_args[1]["env"]
    assert "--headless=new" in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert "--enable-automation" not in args
    assert "--remote-debugging-port=9222" in args
    assert env["HOME"] not in ("", "/")


@pytest.mark.asyncio
async def test_chromium_fetcher_hides_headless_token_with_a_launch_flag(tmp_path):
    """The User-Agent goes in as --user-agent, derived from --version, in the reduced form real Chrome sends.

    Overriding it through CDP without metadata empties navigator.userAgentData.brands and drops the
    sec-ch-ua header, which is a louder bot signal than the HeadlessChrome token it was hiding.
    """
    fake_proc = MagicMock()
    fake_proc.returncode = None

    async def fake_wait():
        return 0

    fake_proc.wait = fake_wait
    version = AsyncMock(return_value="Chromium 151.0.7922.137 built on Debian GNU/Linux 13 (trixie)")
    with patch("shutil.which", return_value="/usr/bin/chromium"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            with patch.object(ChromiumFetcher, "_native_version", new=version):
                with patch.object(ChromiumFetcher, "_connect_cdp", new_callable=AsyncMock):
                    async with ChromiumFetcher(user_data_dir=tmp_path / "p", port=9222) as fetcher:
                        assert fetcher.user_agent_applied_at_launch
                        assert "Chrome/151.0.0.0" in fetcher.user_agent
    flags = [arg for arg in mock_exec.call_args[0] if arg.startswith("--user-agent=")]
    assert len(flags) == 1
    assert "Chrome/151.0.0.0 Safari/537.36" in flags[0] and "Headless" not in flags[0] and "7922" not in flags[0]


@pytest.mark.asyncio
async def test_chromium_fetcher_falls_back_to_protocol_override_when_version_is_unknown(tmp_path):
    native = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36"
    fetcher = ChromiumFetcher(port=9222)
    assert not fetcher.user_agent_applied_at_launch
    with patch.object(ChromiumFetcher, "_wait_until_ready", new=AsyncMock(return_value=("ws://x/devtools/browser/1", native))):
        with patch("crau.fetchers.cdp.CdpBrowserFetcher.__aenter__", new_callable=AsyncMock):
            await fetcher._connect_cdp()
    assert "HeadlessChrome" not in fetcher.user_agent and not fetcher.user_agent_applied_at_launch
