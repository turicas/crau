"""Named browser instances that outlive the process that started them (state files under the state directory)."""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import BrowserError
from .launch import is_running, terminate_process_group
from .paths import state_dir


@dataclass
class Instance:
    name: str
    browser: str
    pid: int
    endpoint: str
    profile: str
    run_dir: str
    temporary_profile: bool
    version: str
    started_at: float
    stealth: bool = True
    insecure: bool = False

    @property
    def alive(self) -> bool:
        return is_running(self.pid)


def _path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise BrowserError(f"Invalid instance name {name!r}: use letters, digits, '.', '_' and '-'")
    return state_dir() / f"{name}.json"


def save(instance: Instance) -> None:
    path = _path(instance.name)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(instance), indent=2), encoding="utf-8")
    temporary.replace(path)


def load(name: str) -> Instance:
    """State of a live instance. A state file whose process died is removed and reported as missing."""
    path = _path(name)
    if not path.exists():
        raise BrowserError(f"No instance named {name!r}. Start one with: crau browser start {name}")
    instance = Instance(**json.loads(path.read_text(encoding="utf-8")))
    if not instance.alive:
        remove(instance)
        raise BrowserError(f"Instance {name!r} is not running anymore (stale state removed)")
    return instance


def list_all() -> list[Instance]:
    instances = []
    for path in sorted(state_dir().glob("*.json")):
        instance = Instance(**json.loads(path.read_text(encoding="utf-8")))
        if instance.alive:
            instances.append(instance)
        else:
            remove(instance)
    return instances


def remove(instance: Instance) -> None:
    _path(instance.name).unlink(missing_ok=True)
    shutil.rmtree(instance.run_dir, ignore_errors=True)


def stop(name: str) -> Instance:
    instance = load(name)
    terminate_process_group(instance.pid)
    remove(instance)
    return instance


def new(name: str, browser: str, process, stealth: bool = True, insecure: bool = False) -> Instance:
    """Build and persist the state for a just-started `BrowserProcess`."""
    instance = Instance(
        name=name,
        browser=browser,
        pid=process.pid,
        endpoint=process.endpoint,
        profile=str(process.profile),
        run_dir=str(process.run_dir),
        temporary_profile=process.temporary_profile,
        version=process.version,
        started_at=time.time(),
        stealth=stealth,
        insecure=insecure,
    )
    save(instance)
    return instance
