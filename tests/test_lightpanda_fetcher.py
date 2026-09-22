import pytest

from crau.browser.errors import LaunchError
from crau.browser.launch import build_command
from crau.fetchers.lightpanda import LightpandaFetcher


@pytest.mark.asyncio
async def test_lightpanda_fetcher_missing_binary_raises_error():
    fetcher = LightpandaFetcher(binary_path="/nonexistent/lightpanda")
    with pytest.raises(LaunchError, match="/nonexistent/lightpanda"):
        async with fetcher:
            pass


def test_lightpanda_fetcher_passes_its_options_to_the_browser(tmp_path):
    fetcher = LightpandaFetcher(binary_path="lightpanda", host="0.0.0.0", port=9222, user_agent="Mozilla/5.0 crau")
    options = fetcher._create_browser().options
    assert (options.browser, str(options.binary), options.host, options.port) == ("lightpanda", "lightpanda", "0.0.0.0", 9222)
    # Lightpanda refuses a --user-agent that looks like another browser: it is applied over CDP after attaching
    assert options.user_agent is None and fetcher._overrides_user_agent_by_protocol
    assert build_command(options, "/usr/bin/lightpanda", tmp_path)[1:6] == ["serve", "--host", "0.0.0.0", "--port", "9222"]
    assert LightpandaFetcher().port is None
