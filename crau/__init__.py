"""crau: Web archiver, crawler and browser automation.

Names are imported on first use: `import crau` works with no third-party package installed, and
the archiving API (which needs `pip install 'crau[archive]'`) is only loaded when asked for.
"""

from crau.version import __version__

_LAZY = {
    "Crawler": "crau.crawler",
    "Request": "crau.crawler",
    "Response": "crau.crawler",
    "Spider": "crau.crawler",
    "cli": "crau.cli",
    "main": "crau.cli",
}

__all__ = [
    "Crawler",
    "Request",
    "Response",
    "Spider",
    "cli",
    "main",
    "__version__",
]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module 'crau' has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(_LAZY[name]), name)
    globals()[name] = value
    return value
