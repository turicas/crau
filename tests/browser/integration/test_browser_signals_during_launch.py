from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

from crau.browser.launch import group_is_running

ROOT = Path(__file__).resolve().parents[3]
CRAU = [sys.executable, "-m", "crau"]


def browser_groups() -> set[int]:
    listing = subprocess.run(["ps", "-eo", "pgid,comm"], capture_output=True, text=True, timeout=30).stdout
    names = ("chromium", "chrome", "firefox", "firefox-bin", "lightpanda")
    return {int(line.split()[0]) for line in listing.splitlines()[1:] if line.split()[1] in names}


def test_sigterm_at_any_moment_of_the_launch_leaves_no_browser(browser_name, site):
    """Kill the command at several points of its start-up: whatever it had launched by then must be gone."""
    before = browser_groups()
    for delay in (0.15, 0.3, 0.6, 1.0, 1.5):
        command = [*CRAU, "page", "html", site.url + "/final", "-b", browser_name, "-s", "#never", "-t", "60"]
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, cwd=ROOT
        )
        time.sleep(delay)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) in (143, -signal.SIGTERM)  # -15 only before Python installed any handler
        deadline = time.perf_counter() + 10
        while time.perf_counter() < deadline and any(group_is_running(group) for group in browser_groups() - before):
            time.sleep(0.1)
        leftovers = {group for group in browser_groups() - before if group_is_running(group)}
        assert not leftovers, f"browser left running after SIGTERM at {delay}s"
