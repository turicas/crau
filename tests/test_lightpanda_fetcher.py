import asyncio
import subprocess
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from crau.fetchers.lightpanda import LightpandaFetcher


@pytest.mark.asyncio
async def test_lightpanda_fetcher_missing_binary_raises_error():
    fetcher = LightpandaFetcher(binary_path="/nonexistent/lightpanda")
    with pytest.raises(FileNotFoundError, match="Lightpanda binary not found"):
        async with fetcher:
            pass


@pytest.mark.asyncio
async def test_lightpanda_fetcher_process_lifecycle():
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()

    async def fake_wait():
        return 0

    fake_proc.wait = fake_wait

    with patch("shutil.which", return_value="/usr/bin/lightpanda"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            with patch.object(LightpandaFetcher, "_wait_until_ready", new_callable=AsyncMock):
                with patch("websockets.connect", new_callable=AsyncMock):
                    fetcher = LightpandaFetcher(binary_path="lightpanda", port=9222)
                    async with fetcher:
                        assert fetcher._proc is fake_proc
                    fake_proc.terminate.assert_called_once()
