import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from crau.fetchers.firefox import FirefoxFetcher


def test_firefox_fetcher_missing_binary():
    with patch("shutil.which", return_value=None):
        fetcher = FirefoxFetcher(binary_path="nonexistent-firefox")
        with pytest.raises(FileNotFoundError, match="Firefox binary not found"):
            fetcher._resolve_binary()


@pytest.mark.asyncio
async def test_firefox_fetcher_prepares_user_js_and_profile(tmp_path):
    custom_profile = tmp_path / "custom-firefox-profile"

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()

    async def fake_wait():
        return 0

    fake_proc.wait = fake_wait

    with patch("shutil.which", return_value="/usr/bin/firefox-esr"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            with patch.object(FirefoxFetcher, "_wait_until_ready", new_callable=AsyncMock):
                with patch.object(FirefoxFetcher, "_init_bidi_session", new_callable=AsyncMock):
                    fetcher = FirefoxFetcher(user_data_dir=custom_profile, port=9223)
                    async with fetcher:
                        assert fetcher.user_data_dir == custom_profile
                        assert not fetcher._is_temp_profile

                    # user.js must exist and contain the 3 remote preferences
                    user_js = custom_profile / "user.js"
                    assert user_js.exists()
                    content = user_js.read_text()
                    assert 'user_pref("remote.active-protocols", 3);' in content
                    assert 'user_pref("devtools.debugger.remote-enabled", true);' in content
                    assert 'user_pref("devtools.debugger.prompt-connection", false);' in content

                    cmd_args = mock_exec.call_args[0]
                    assert "-profile" in cmd_args
                    assert str(custom_profile) in cmd_args
                    fake_proc.terminate.assert_called_once()
