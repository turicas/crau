from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from crau.browser.errors import LaunchError
from crau.browser.launch import LaunchOptions, build_command, find_binary, firefox_user_js, stealth_user_agent

BINARY = Path("/opt/browser")
PROFILE = Path("/tmp/profile")


def test_stealth_user_agent_uses_reduced_version_of_the_same_major():
    agent = stealth_user_agent("Chromium 151.0.7922.137 built on Debian GNU/Linux 13 (trixie)")
    assert agent.endswith("Chrome/151.0.0.0 Safari/537.36") and "Headless" not in agent
    assert stealth_user_agent("no version here") is None


def test_chromium_command_default_is_headless_stealth_on_port_zero():
    command = build_command(LaunchOptions(), BINARY, PROFILE, "Chromium 151.0.7922.137")
    assert command[0] == str(BINARY) and command[-1] == "about:blank"
    assert "--remote-debugging-port=0" in command and f"--user-data-dir={PROFILE}" in command
    assert "--headless=new" in command and "--no-sandbox" in command and "--disable-crash-reporter" in command
    assert "--disable-blink-features=AutomationControlled" in command
    assert any(arg.startswith("--user-agent=") and "Chrome/151.0.0.0" in arg for arg in command)
    assert "--window-size=1440,900" in command


def test_chromium_command_without_stealth_headed_sandboxed_with_extras():
    options = LaunchOptions(
        stealth=False, headless=False, sandbox=True, extra_args=["--lang=pt-BR"], viewport=(800, 600)
    )
    command = build_command(options, BINARY, PROFILE, "Chromium 151.0.7922.137")
    joined = " ".join(command)
    assert "AutomationControlled" not in joined and "--user-agent" not in joined and "--headless" not in joined
    assert "--no-sandbox" not in command and "--lang=pt-BR" in command and "--window-size=800,600" in command


def test_explicit_user_agent_wins_over_stealth_one():
    command = build_command(LaunchOptions(user_agent="Custom/1.0"), BINARY, PROFILE, "Chromium 151.0.7922.137")
    assert "--user-agent=Custom/1.0" in command and not any("Chrome/151" in arg for arg in command)


def test_firefox_command_and_preferences():
    options = LaunchOptions(
        browser="firefox", user_agent="Custom/1.0", prefs={"intl.accept_languages": "pt-BR", "x.n": 3}
    )
    command = build_command(options, BINARY, PROFILE)
    assert (
        command[:4] == [str(BINARY), "--remote-debugging-port=0", "-profile", str(PROFILE)] and "--headless" in command
    )
    user_js = firefox_user_js(options)
    assert 'user_pref("devtools.jsonview.enabled", false);' in user_js
    assert 'user_pref("general.useragent.override", "Custom/1.0");' in user_js
    assert 'user_pref("intl.accept_languages", "pt-BR");' in user_js and 'user_pref("x.n", 3);' in user_js


def test_lightpanda_command_serves_cdp_on_port_zero():
    command = build_command(LaunchOptions(browser="lightpanda"), BINARY, PROFILE)
    assert command[:6] == [str(BINARY), "serve", "--host", "127.0.0.1", "--port", "0"]


def test_find_binary_prefers_explicit_then_environment_then_path(tmp_path, monkeypatch):
    def executable(name: str) -> Path:
        path = tmp_path / name
        path.write_text("#!/bin/sh\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    explicit, from_env = executable("explicit"), executable("from-env")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "firefox-esr").write_text("#!/bin/sh\n")
    (bin_dir / "firefox-esr").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CRAU_FIREFOX", raising=False)
    assert find_binary("firefox") == bin_dir / "firefox-esr"
    monkeypatch.setenv("CRAU_FIREFOX", str(from_env))
    assert find_binary("firefox") == from_env
    assert find_binary("firefox", explicit) == explicit


def test_find_binary_errors_are_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CRAU_CHROMIUM", raising=False)
    with pytest.raises(LaunchError, match="crau browser install chromium"):
        find_binary("chromium")
    with pytest.raises(LaunchError, match="does not exist"):
        find_binary("chromium", tmp_path / "missing")
    with pytest.raises(LaunchError, match="Unknown browser"):
        find_binary("netscape")
    assert os.environ["PATH"] == str(tmp_path)
