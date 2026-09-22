"""Where state, cache and the browser's HOME live, with fallbacks for containers.

Containers frequently run with `HOME=/`, `HOME` unset or the XDG variables empty. Every helper here returns a directory
that exists and is writable, trying in order: the XDG variable (absolute paths only, as the XDG spec demands), the
conventional directory under `HOME`, and finally a per-user directory under the system temp dir.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

APP_NAME = "crau"


def _usable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK | os.X_OK)


def _home() -> Path | None:
    value = os.environ.get("HOME", "").strip()
    if not value or value == "/":
        return None
    return Path(value)


def _resolve(variable: str, home_suffix: str) -> Path:
    candidates = []
    value = os.environ.get(variable, "").strip()
    if value and Path(value).is_absolute():
        candidates.append(Path(value) / APP_NAME)
    home = _home()
    if home is not None:
        candidates.append(home / home_suffix / APP_NAME)
    candidates.append(Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.getuid()}" / home_suffix.replace("/", "-"))
    for candidate in candidates:
        if _usable(candidate):
            return candidate
    raise OSError(f"No writable directory found for {variable} (tried: {', '.join(map(str, candidates))})")


def state_dir() -> Path:
    """Named-instance state files: `$XDG_STATE_HOME`, else `~/.local/state`, else the temp dir."""
    return _resolve("XDG_STATE_HOME", ".local/state")


def cache_dir() -> Path:
    """Logs, temporary profiles, downloaded binaries: `$XDG_CACHE_HOME`, else `~/.cache`, else the temp dir."""
    return _resolve("XDG_CACHE_HOME", ".cache")


def browser_home() -> Path:
    """HOME handed to the browser process.

    Chromium writes crash and config data under HOME at startup; with an unwritable HOME (`/` is typical in containers)
    it may abort and leave core dumps behind. Keep the real HOME when it is usable, otherwise provide a private one.
    """
    home = _home()
    if home is not None and home.is_dir() and os.access(home, os.W_OK | os.X_OK):
        return home
    fallback = cache_dir() / "home"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback
