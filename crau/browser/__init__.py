"""Drive real browsers (Chromium, Firefox, Lightpanda) over CDP and WebDriver BiDi with the standard library only.

    from crau.browser import Browser

Names are imported on first use so that `crau --help` stays fast.
"""

from __future__ import annotations

_EXPORTS = {
    "Browser": "session",
    "Response": "session",
    "Tab": "session",
    "__version__": "session",
    "BrowserError": "errors",
    "JsError": "errors",
    "LaunchError": "errors",
    "NavigationError": "errors",
    "ProtocolError": "errors",
    "UnsupportedError": "errors",
    "WaitTimeout": "errors",
    "instances": None,
}
__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module 'browser' has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f".{_EXPORTS[name] or name}", __name__)
    value = module if _EXPORTS[name] is None else getattr(module, name)
    globals()[name] = value
    return value
