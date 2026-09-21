from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from crau.fetchers.base import browser_log_tail, browser_port_from_log, create_browser_log
from crau.fetchers.cdp import CdpBrowserFetcher


LISTENING_PATTERN = r"address=[^:\s]+:(\d+)"


class LightpandaFetcher(CdpBrowserFetcher):
    """High-performance headless browser fetcher powered by Lightpanda."""

    def __init__(
        self,
        binary_path: str = "lightpanda",
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float = 30.0,
        user_agent: str | None = None,
        **settle_options: Any,
    ):
        self.binary_path = binary_path
        self.host = host
        self.port = port  # None: the browser picks one (--port 0) and announces it
        self._proc: asyncio.subprocess.Process | None = None
        self._log_path: Path | None = None

        endpoint_url = f"ws://{self.host}:{self.port or 0}"  # replaced once the browser is up
        super().__init__(endpoint_url=endpoint_url, timeout=timeout, user_agent=user_agent, **settle_options)

    def _resolve_binary(self) -> str:
        # Check explicit path or PATH
        if Path(self.binary_path).is_file():
            return str(Path(self.binary_path).resolve())
        resolved = shutil.which(self.binary_path)
        if resolved:
            return resolved
        raise FileNotFoundError(
            f"Lightpanda binary not found at '{self.binary_path}'. "
            "Install lightpanda via 'pip install crau[lightpanda]' or download from https://lightpanda.io"
        )

    async def _wait_until_ready(self, timeout: float = 5.0) -> None:
        start_time = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_time < timeout:
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError(
                    f"Lightpanda process exited prematurely with code {self._proc.returncode}"
                    + browser_log_tail(self._log_path)
                )
            if self.port is None:
                self.port = browser_port_from_log(self._log_path, LISTENING_PATTERN)
                if self.port is None:
                    await asyncio.sleep(0.05)
                    continue
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=0.2,
                )
                writer.close()
                await writer.wait_closed()
                return
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.05)
        raise TimeoutError(
            f"Lightpanda daemon did not become ready on port {self.port} within {timeout}s"
            + browser_log_tail(self._log_path)
        )

    async def __aenter__(self) -> "LightpandaFetcher":
        resolved_bin = self._resolve_binary()
        cmd = [resolved_bin, "serve", "--host", self.host, "--port", str(self.port or 0)]
        self._log_path = create_browser_log("lightpanda")
        log = self._log_path.open("wb")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log,
            stderr=log,
        )
        log.close()
        try:
            await self._wait_until_ready()
            self.endpoint_url = f"ws://{self.host}:{self.port}"
            await super().__aenter__()
            return self
        except Exception:
            await self._stop_process()
            raise

    async def _stop_process(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
            finally:
                self._proc = None

        if self._log_path is not None:
            self._log_path.unlink(missing_ok=True)
            self._log_path = None

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            await super().__aexit__(exc_type, exc_val, exc_tb)
        finally:
            await self._stop_process()
