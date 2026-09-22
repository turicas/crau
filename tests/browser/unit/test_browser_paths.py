from __future__ import annotations

import os

import pytest
from crau.browser import paths


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    return monkeypatch


def test_xdg_state_home_is_used_when_set(clean_env, tmp_path):
    clean_env.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert paths.state_dir() == tmp_path / "state" / "crau"


@pytest.mark.parametrize("value", ["", "   ", "relative/path"])
def test_blank_or_relative_xdg_value_falls_back_to_home(clean_env, tmp_path, value):
    clean_env.setenv("XDG_STATE_HOME", value)
    clean_env.setenv("HOME", str(tmp_path / "home"))
    assert paths.state_dir() == tmp_path / "home" / ".local/state" / "crau"


@pytest.mark.parametrize("home", [None, "", "/"])
def test_without_usable_home_everything_goes_to_temp_dir(clean_env, tmp_path, home):
    if home is not None:
        clean_env.setenv("HOME", home)
    for directory in (paths.state_dir(), paths.cache_dir(), paths.browser_home()):
        assert str(directory).startswith(str(tmp_path / "tmp"))
        assert directory.is_dir() and os.access(directory, os.W_OK)


def test_unwritable_xdg_directory_is_skipped(clean_env, tmp_path):
    if os.getuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    clean_env.setenv("XDG_CACHE_HOME", str(locked))
    assert str(paths.cache_dir()).startswith(str(tmp_path / "tmp"))


def test_browser_home_keeps_a_writable_home(clean_env, tmp_path):
    clean_env.setenv("HOME", str(tmp_path))
    assert paths.browser_home() == tmp_path
