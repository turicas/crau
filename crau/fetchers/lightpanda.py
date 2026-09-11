from __future__ import annotations

import asyncio
import os
import shutil
import socket
from pathlib import Path
from typing import Any

from crau.fetchers.cdp import CdpBrowserFetcher


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


class LightpandaFetcher(CdpBrowserFetcher):
    """High-performance headless browser fetcher powered by Lightpanda."""

    def __init__(
        self,
        binary_path: str = "lightpanda",
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float = 30.0,
        user_agent: str | None = None,
    ):
        self.binary_path = binary_path
        self.host = host
        self.port = port or _find_free_port()
        self._proc: asyncio.subprocess.Process | None = None

        endpoint_url = f"ws://{self.host}:{self.port}"
        super().__init__(endpoint_url=endpoint_url, timeout=timeout, user_agent=user_agent)

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
                )
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
        raise TimeoutError(f"Lightpanda daemon did not become ready on port {self.port} within {timeout}s")

    async def __aenter__(self) -> "LightpandaFetcher":
        resolved_bin = self._resolve_binary()
        cmd = [resolved_bin, "serve", "--host", self.host, "--port", str(self.port)]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._wait_until_ready()
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

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            await super().__aexit__(exc_type, exc_val, exc_tb)
        finally:
            await self._stop_process()
