from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from crau.fetchers.cdp import HEADLESS_UA_TOKEN, CdpBrowserFetcher

logger = logging.getLogger("crau")

# Chrome's reduced User-Agent freezes everything but the major version; these are the per-platform constants
_UA_PLATFORMS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "win32": "Windows NT 10.0; Win64; x64",
}
_UA_DEFAULT_PLATFORM = "X11; Linux x86_64"


def _reduced_user_agent(version_text: str) -> str | None:
    """User-Agent a regular (non-headless) Chrome of this version sends, or None when the version is unknown."""
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", version_text)
    if not match:
        return None
    platform = _UA_PLATFORMS.get(sys.platform, _UA_DEFAULT_PLATFORM)
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{match.group(1)}.0.0.0 Safari/537.36"
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


class ChromiumFetcher(CdpBrowserFetcher):
    """Headless Chromium / Google Chrome fetcher communicating via CDP."""

    def __init__(
        self,
        binary_path: str | Path | None = None,
        user_data_dir: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
        window_size: tuple[int, int] = (1440, 900),
        timeout: float = 30.0,
        user_agent: str | None = None,
        extra_args: list[str] | None = None,
        **settle_options: Any,
    ):
        self.binary_path = str(binary_path) if binary_path else None
        self.host = host
        self.port = port or _find_free_port()
        self.window_size = window_size
        self.extra_args = extra_args or []

        if user_data_dir:
            self.user_data_dir = Path(user_data_dir).resolve()
            self._is_temp_profile = False
        else:
            self.user_data_dir = Path(tempfile.mkdtemp(prefix="crau-chromium-"))
            self._is_temp_profile = True

        self._proc: asyncio.subprocess.Process | None = None
        super().__init__(
            endpoint_url=f"ws://{self.host}:{self.port}",
            timeout=timeout,
            user_agent=user_agent,
            **settle_options,
        )

    def _resolve_binary(self) -> str:
        candidates = (
            [self.binary_path]
            if self.binary_path
            else ["chromium", "google-chrome", "chromium-browser", "chrome"]
        )
        for name in candidates:
            if not name:
                continue
            if Path(name).is_file():
                return str(Path(name).resolve())
            found = shutil.which(name)
            if found:
                return found

        raise FileNotFoundError(
            "Chromium binary not found. Install it via 'apt install -y chromium' (Debian/Ubuntu) "
            "or specify --binary-path."
        )

    async def _native_version(self, binary: str, env: dict[str, str]) -> str:
        """Output of `<binary> --version` ('' when it cannot be obtained)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            return output.decode("utf-8", errors="replace").strip()
        except Exception as error:
            logger.debug("Could not read the version of %s: %s", binary, error)
            return ""

    async def _wait_until_ready(self, timeout: float | None = None) -> tuple[str, str]:
        """Poll /json/version until the browser answers; returns (websocket URL, native User-Agent)."""
        url = f"http://{self.host}:{self.port}/json/version"
        # a cold start (binary not in the page cache) can take >10s
        timeout = max(self.timeout, 30.0) if timeout is None else timeout
        start_time = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_time < timeout:
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError(
                    f"Chromium exited prematurely with code {self._proc.returncode}"
                )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "crau"})
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode())
                        ws_url = data.get("webSocketDebuggerUrl")
                        if ws_url:
                            return ws_url, data.get("User-Agent", "")
            except Exception:
                await asyncio.sleep(0.1)

        raise TimeoutError(f"Chromium did not become ready on port {self.port} within {timeout}s")

    async def _connect_cdp(self) -> None:
        ws_url, native_user_agent = await self._wait_until_ready()
        self.endpoint_url = ws_url
        # only reached when the version was unknown at launch (see __aenter__): fall back to a protocol
        # override with the browser's own UA minus the token, at the cost of the client hints
        if not self.user_agent and HEADLESS_UA_TOKEN in native_user_agent:
            self.user_agent = native_user_agent.replace(HEADLESS_UA_TOKEN, "Chrome")
        await super().__aenter__()

    async def __aenter__(self) -> "ChromiumFetcher":
        resolved_bin = self._resolve_binary()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        if not env.get("HOME") or env["HOME"] == "/":
            env["HOME"] = "/tmp"

        cmd = [
            resolved_bin,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-crash-reporter",
            # inert on full Chromium without --enable-automation, but chrome-headless-shell
            # turns AutomationControlled on by default (navigator.webdriver === true)
            "--disable-blink-features=AutomationControlled",
            f"--remote-debugging-port={self.port}",
            f"--window-size={self.window_size[0]},{self.window_size[1]}",
            f"--user-data-dir={self.user_data_dir}",
        ]
        # Headless Chromium advertises "HeadlessChrome/..." which anti-bot systems key on. The flag keeps the
        # header, navigator.userAgent and the sec-ch-ua client hints coherent; a CDP override does not.
        if not self.user_agent:
            self.user_agent = _reduced_user_agent(await self._native_version(resolved_bin, env))
        if self.user_agent:
            cmd.append(f"--user-agent={self.user_agent}")
            self.user_agent_applied_at_launch = True
        cmd.extend(self.extra_args)
        cmd.append("about:blank")

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await self._connect_cdp()
            return self
        except Exception:
            await self._stop_process()
            raise

    async def _stop_process(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
            finally:
                self._proc = None

        if self._is_temp_profile and self.user_data_dir.exists():
            shutil.rmtree(self.user_data_dir, ignore_errors=True)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            await super().__aexit__(exc_type, exc_val, exc_tb)
        finally:
            await self._stop_process()
