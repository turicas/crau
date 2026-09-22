from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from crau.browser import instances
from crau.browser.errors import BrowserError


@pytest.fixture(autouse=True)
def state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def fake_process(pid: int, tmp_path) -> SimpleNamespace:
    run_dir = tmp_path / f"run-{pid}"
    run_dir.mkdir()
    return SimpleNamespace(
        pid=pid, endpoint="ws://127.0.0.1:1/x", profile=run_dir / "profile", run_dir=run_dir, temporary_profile=True,
        version="Test 1.0",
    )  # fmt: skip


def test_save_load_list_round_trip_for_a_live_process(tmp_path):
    created = instances.new("work", "chromium", fake_process(os.getpid(), tmp_path), stealth=False)
    loaded = instances.load("work")
    assert loaded == created and loaded.alive and loaded.stealth is False
    assert [instance.name for instance in instances.list_all()] == ["work"]


def test_dead_instance_is_reported_missing_and_cleaned_up(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    instance = instances.new("gone", "firefox", fake_process(child.pid, tmp_path))
    with pytest.raises(BrowserError, match="not running"):
        instances.load("gone")
    assert instances.list_all() == [] and not (tmp_path / f"run-{child.pid}").exists()
    assert not instance.alive


def test_stop_terminates_the_whole_process_group(tmp_path):
    sleeper = "import time; time.sleep(60)"
    script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', '{sleeper}']); time.sleep(60)"
    parent = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    instances.new("group", "chromium", fake_process(parent.pid, tmp_path))
    from crau.browser.launch import group_is_running

    assert group_is_running(parent.pid)
    instances.stop("group")
    parent.wait(timeout=5)
    assert not group_is_running(parent.pid)
    with pytest.raises(BrowserError, match="No instance"):
        instances.load("group")


@pytest.mark.parametrize("name", ["", "../etc", "a b", "-dash", "x" * 80])
def test_unsafe_instance_names_are_rejected(name):
    with pytest.raises(BrowserError, match="Invalid instance name"):
        instances.load(name)
