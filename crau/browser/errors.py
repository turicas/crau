"""Exceptions raised by the library. All of them derive from `BrowserError`."""

from __future__ import annotations


class BrowserError(Exception):
    """Base class for every error raised by this package."""


class LaunchError(BrowserError):
    """The browser binary is missing, exited early or never opened its automation endpoint."""


class ProtocolError(BrowserError):
    """The browser answered a CDP or BiDi command with an error."""

    def __init__(self, method: str, code: str | int | None, message: str) -> None:
        super().__init__(f"{method}: {code}: {message}" if code not in (None, "") else f"{method}: {message}")
        self.method = method
        self.code = code
        self.message = message


class JsError(BrowserError):
    """JavaScript evaluated in the page threw an exception."""


class NavigationError(BrowserError):
    """The page could not be loaded (DNS failure, refused connection, invalid URL...)."""


class WaitTimeout(BrowserError, TimeoutError):
    """A wait condition (selector, expression, network idle) was not met in time."""


class UnsupportedError(BrowserError):
    """The chosen browser (or its version) cannot do what was asked."""
