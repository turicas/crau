from __future__ import annotations

import asyncio
import datetime
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from crau.fetchers.base import Fetcher
from crau.models import CrawlResult, NetworkTransaction, Page, RawRequest, RawResponse

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


class FirefoxFetcher(Fetcher):
    """Cross-browser headless Firefox fetcher communicating via W3C WebDriver BiDi."""

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
    ):
        if websockets is None:
            raise ImportError(
                "The 'websockets' library is required for browser fetchers. "
                "Install it via 'pip install crau[browser]'."
            )
        self.binary_path = str(binary_path) if binary_path else None
        self.host = host
        self.port = port or _find_free_port()
        self.window_size = window_size
        self.timeout = timeout
        self.user_agent = user_agent
        self.extra_args = extra_args or []

        if user_data_dir:
            self.user_data_dir = Path(user_data_dir).resolve()
            self._is_temp_profile = False
        else:
            self.user_data_dir = Path(tempfile.mkdtemp(prefix="crau-firefox-"))
            self._is_temp_profile = True

        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._msg_id = 0
        self._context_id: str | None = None
        self._session_id: str | None = None

    def _resolve_binary(self) -> str:
        candidates = (
            [self.binary_path]
            if self.binary_path
            else ["firefox-esr", "firefox"]
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
            "Firefox binary not found. Install it via 'apt install -y firefox-esr' (Debian/Ubuntu) "
            "or specify --binary-path."
        )

    def _prepare_profile(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        user_js = self.user_data_dir / "user.js"
        # Always ensure mandatory remote debugging preferences exist
        prefs = (
            'user_pref("remote.active-protocols", 3);\n'
            'user_pref("devtools.debugger.remote-enabled", true);\n'
            'user_pref("devtools.debugger.prompt-connection", false);\n'
        )
        if user_js.exists():
            current_content = user_js.read_text(encoding="utf-8")
            if "remote.active-protocols" not in current_content:
                user_js.write_text(current_content + "\n" + prefs, encoding="utf-8")
        else:
            user_js.write_text(prefs, encoding="utf-8")

    async def _wait_until_ready(self, timeout: float = 10.0) -> None:
        start_time = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_time < timeout:
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError(
                    f"Firefox exited prematurely with code {self._proc.returncode}"
                )
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=0.3,
                )
                writer.close()
                await writer.wait_closed()
                return
            except Exception:
                await asyncio.sleep(0.1)

        raise TimeoutError(f"Firefox BiDi did not open port {self.port} within {timeout}s")

    async def _send_bidi(self, method: str, params: dict[str, Any] | None = None) -> dict:
        self._msg_id += 1
        cmd_id = self._msg_id
        payload = {"id": cmd_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))

        while True:
            resp_raw = await self._ws.recv()
            data = json.loads(resp_raw)
            if data.get("id") == cmd_id:
                if "error" in data or data.get("type") == "error":
                    raise RuntimeError(f"BiDi error on {method}: {data}")
                return data.get("result", {})

    async def _init_bidi_session(self) -> None:
        ws_url = f"ws://{self.host}:{self.port}/session"
        self._ws = await websockets.connect(ws_url)

        # 1. Establish BiDi session
        try:
            res = await self._send_bidi("session.new", {"capabilities": {}})
            self._session_id = res.get("sessionId")
        except Exception:
            pass

        # 2. Create browsing context (tab)
        created = await self._send_bidi("browsingContext.create", {"type": "tab"})
        self._context_id = created.get("context")

        # 3. Set viewport
        try:
            await self._send_bidi(
                "browsingContext.setViewport",
                {
                    "context": self._context_id,
                    "viewport": {
                        "width": self.window_size[0],
                        "height": self.window_size[1],
                    },
                },
            )
        except Exception:
            pass

        # 4. Anti-detection: neutralize navigator.webdriver
        try:
            await self._send_bidi(
                "script.evaluate",
                {
                    "expression": "Object.defineProperty(navigator, 'webdriver', { get: () => false })",
                    "target": {"context": self._context_id},
                    "awaitPromise": True,
                    "resultOwnership": "none",
                },
            )
        except Exception:
            pass

    async def __aenter__(self) -> "FirefoxFetcher":
        resolved_bin = self._resolve_binary()
        self._prepare_profile()

        env = os.environ.copy()
        if not env.get("HOME") or env["HOME"] == "/":
            env["HOME"] = "/tmp"

        cmd = [
            resolved_bin,
            "--headless",
            "--width",
            str(self.window_size[0]),
            "--height",
            str(self.window_size[1]),
            f"--remote-debugging-port={self.port}",
            "-profile",
            str(self.user_data_dir),
        ]
        cmd.extend(self.extra_args)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await self._wait_until_ready()
            await self._init_bidi_session()
            return self
        except Exception:
            await self._stop_process()
            raise

    async def fetch(self, url: str, **kwargs: Any) -> CrawlResult:
        if not self._ws or not self._context_id:
            raise RuntimeError("Firefox BiDi session is not ready.")

        start_time = time.perf_counter()

        # Navigate
        await self._send_bidi(
            "browsingContext.navigate",
            {
                "context": self._context_id,
                "url": url,
                "wait": "complete",
            },
        )

        # Retrieve rendered HTML DOM
        eval_res = await self._send_bidi(
            "script.evaluate",
            {
                "expression": "document.documentElement.outerHTML",
                "target": {"context": self._context_id},
                "awaitPromise": True,
                "resultOwnership": "none",
            },
        )
        rendered_html = (
            eval_res.get("result", {}).get("value")
            or eval_res.get("result", {}).get("description")
            or ""
        )

        duration = time.perf_counter() - start_time
        now = datetime.datetime.now(datetime.timezone.utc)

        raw_req = RawRequest(
            url=url,
            method="GET",
            raw_headers=[(b"User-Agent", (self.user_agent or "crau/firefox").encode("latin1"))],
            timestamp=now,
        )
        raw_resp = RawResponse(
            status_code=200,
            reason_phrase="OK",
            http_version="HTTP/1.1",
            raw_headers=[(b"Content-Type", b"text/html; charset=utf-8")],
            raw_body=rendered_html.encode("utf-8"),
            timestamp=now,
        )
        tx = NetworkTransaction(
            request=raw_req,
            response=raw_resp,
            duration_seconds=duration,
        )

        page = Page(
            url=url,
            status_code=200,
            content=rendered_html,
            raw_body=raw_resp.raw_body,
            transactions=[tx],
            timing={"total": duration},
        )

        return CrawlResult(transactions=[tx], page=page)

    async def _stop_process(self) -> None:
        if self._ws is not None:
            if self._session_id:
                try:
                    await self._send_bidi("session.end", {})
                except Exception:
                    pass
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

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
        await self._stop_process()
