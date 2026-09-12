import asyncio
from pathlib import Path
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
