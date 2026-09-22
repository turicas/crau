"""Find, start and stop browser processes, and discover the automation endpoint each one opens.

Every browser is started with port 0 so parallel runs never collide; the real endpoint is read from the file (or log
line) the browser writes. Processes get their own process group, a writable HOME, a private working directory and a
zero core-dump limit, so a crashing browser can neither leave orphans nor drop `core` files into the caller's project.
"""

from __future__ import annotations

import asyncio
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LaunchError
from .paths import browser_home, cache_dir

BROWSERS = ("chromium", "firefox", "lightpanda")
PROTOCOLS = {"chromium": "cdp", "firefox": "bidi", "lightpanda": "cdp"}
BINARY_NAMES = {
    "chromium": ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"),
    "firefox": ("firefox", "firefox-esr"),
    "lightpanda": ("lightpanda",),
}
DEFAULT_VIEWPORT = (1440, 900)
_FIREFOX_PREFS = {
    # application/json documents would otherwise be replaced by the built-in JSON viewer DOM
    "devtools.jsonview.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "app.update.auto": False,
}
_PREFS_MARKER = "// crau"


@dataclass
class LaunchOptions:
    browser: str = "chromium"
    binary: Path | None = None
    headless: bool = True
    stealth: bool = True
    profile: Path | None = None
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    user_agent: str | None = None
    sandbox: bool = False
    insecure: bool = False
    extra_args: list[str] = field(default_factory=list)
    prefs: dict[str, object] = field(default_factory=dict)
    timeout: float = 30.0


def find_binary(browser: str, explicit: Path | None = None) -> Path:
    """Explicit path, then `CRAU_<BROWSER>`, then PATH and `~/.local/bin` (where `install.py` links)."""
    if browser not in BROWSERS:
        raise LaunchError(f"Unknown browser {browser!r}. Options: {', '.join(BROWSERS)}")
    configured = explicit or os.environ.get(f"CRAU_{browser.upper()}")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise LaunchError(f"{browser} binary {str(path)!r} does not exist or is not executable")
    local_bin = Path.home() / ".local" / "bin"
    for name in BINARY_NAMES[browser]:
        found = shutil.which(name) or (str(local_bin / name) if (local_bin / name).is_file() else None)
        if found:
            return Path(found)
    raise LaunchError(f"No {browser} binary found; install one with: crau browser install {browser}")


async def binary_version(binary: Path, timeout: float = 15.0) -> str:
    """Output of `<binary> --version` (`version` for Lightpanda), e.g. 'Chromium 151.0.7922.137 built on Debian'."""
    argument = "version" if "lightpanda" in binary.name else "--version"
    environment = {**os.environ, "HOME": str(browser_home()), "LIGHTPANDA_DISABLE_TELEMETRY": "true"}
    process = await asyncio.create_subprocess_exec(
        str(binary), argument, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=environment
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise LaunchError(f"{binary} --version did not answer in {timeout}s") from None
    lines = output.decode("utf-8", "replace").strip().splitlines()
    return lines[-1].strip() if lines else ""


def stealth_user_agent(version_text: str) -> str | None:
    """Regular-Chrome User-Agent for this build, in the reduced `Chrome/<major>.0.0.0` form real Chrome sends.

    Headless Chromium announces `HeadlessChrome/...`. Passing the result as `--user-agent` keeps the header, the JS
    value and the `sec-ch-ua` client hints coherent; overriding through the protocol without metadata empties the
    client hints instead.
    """
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", version_text)
    if not match:
        return None
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{match.group(1)}.0.0.0 Safari/537.36"
    )


def build_command(options: LaunchOptions, binary: Path, profile: Path, version_text: str = "") -> list[str]:
    """Command line for the browser. Pure function: no filesystem or process access."""
    width, height = options.viewport
    if options.browser == "chromium":
        command = [
            str(binary),
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-crash-reporter",
            "--disable-dev-shm-usage",
            "--password-store=basic",
        ]
        if options.headless:
            command += ["--headless=new", "--disable-gpu"]
        if not options.sandbox:
            command.append("--no-sandbox")
        if options.insecure:
            command.append("--ignore-certificate-errors")
        user_agent = options.user_agent
        if options.stealth:
            command.append("--disable-blink-features=AutomationControlled")
            if user_agent is None and options.headless:
                user_agent = stealth_user_agent(version_text)
        if user_agent:
            command.append(f"--user-agent={user_agent}")
        return command + list(options.extra_args) + ["about:blank"]
    if options.browser == "firefox":
        command = [str(binary), "--remote-debugging-port=0", "-profile", str(profile), "-no-remote", "-new-instance"]
        command += ["--width", str(width), "--height", str(height)]
        if options.headless:
            command.append("--headless")
        return command + list(options.extra_args) + ["about:blank"]
    command = [str(binary), "serve", "--host", "127.0.0.1", "--port", "0", "--log-level", "info"]
    if options.user_agent:
        command += ["--user-agent", options.user_agent]
    if options.insecure:
        command.append("--insecure-disable-tls-host-verification")
    return command + list(options.extra_args)


def firefox_user_js(options: LaunchOptions) -> str:
    """Preferences block appended to the profile's `user.js`."""
    prefs: dict[str, object] = dict(_FIREFOX_PREFS)
    if options.user_agent:
        prefs["general.useragent.override"] = options.user_agent
    prefs.update(options.prefs)
    lines = [_PREFS_MARKER]
    for name, value in prefs.items():
        rendered = str(value).lower() if isinstance(value, bool) else value if isinstance(value, int) else None
        if rendered is None:
            rendered = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f'user_pref("{name}", {rendered});')
    return "\n".join(lines) + "\n"


def _limit_core_dumps() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _stat_fields(pid: int | str) -> list[str]:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return []


def is_running(pid: int) -> bool:
    """False for missing and for zombie processes (a zombie only waits for its parent to read the exit status)."""
    fields = _stat_fields(pid)
    return bool(fields) and fields[0] != "Z"


def group_is_running(group: int) -> bool:
    """True while any live (non-zombie) process belongs to the process group."""
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat_fields(entry.name)
            if len(fields) > 2 and fields[0] != "Z" and fields[2] == str(group):
                return True
    return False


def _wait_until(condition, timeout: float) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def terminate_process_group(pid: int, grace: float = 5.0) -> None:
    """SIGTERM the browser's group, let the main process and then its helpers finish, SIGKILL whatever is left.

    Helpers get their own grace period: Chromium's network service is the one that flushes cookies to disk. The exit
    status is left for whoever owns the child (asyncio reaps it), hence /proc instead of waitpid.
    """
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return
    _wait_until(lambda: not is_running(pid), grace)
    if not _wait_until(lambda: not group_is_running(group), 2.0):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass


class BrowserProcess:
    """A running browser: `pid`, `endpoint` (WebSocket URL), `profile`, `log_path` and `version`."""

    def __init__(self, options: LaunchOptions) -> None:
        self.options = options
        self.pid: int | None = None
        self.endpoint = ""
        self.version = ""
        self.profile: Path | None = None
        self.run_dir: Path | None = None
        self.temporary_profile = False
        self._process: subprocess.Popen | None = None

    @property
    def log_path(self) -> Path:
        return self.run_dir / "browser.log"

    async def start(self) -> BrowserProcess:
        options = self.options
        binary = find_binary(options.browser, options.binary)
        self.version = await binary_version(binary)
        self.run_dir = Path(tempfile.mkdtemp(prefix=f"{options.browser}-", dir=cache_dir()))
        if options.profile is None:
            self.profile, self.temporary_profile = self.run_dir / "profile", True
        else:
            self.profile = options.profile.expanduser().resolve()
        self.profile.mkdir(parents=True, exist_ok=True)
        self._prepare_profile()
        environment = {**os.environ, "HOME": str(browser_home())}
        environment.setdefault("LIGHTPANDA_DISABLE_TELEMETRY", "true")
        # plain Popen on purpose: asyncio kills its subprocesses when the event loop closes, and a named instance
        # must outlive the process (and the loop) that started it
        with self.log_path.open("wb") as log:
            self._process = subprocess.Popen(
                build_command(options, binary, self.profile, self.version),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=self.run_dir,
                env=environment,
                start_new_session=True,
                preexec_fn=_limit_core_dumps,
            )
        self.pid = self._process.pid
        try:
            self.endpoint = await self._wait_for_endpoint()
        except BaseException:  # includes cancellation and SystemExit raised by a signal handler
            self.stop()
            raise
        return self

    def _prepare_profile(self) -> None:
        if self.options.browser == "chromium":
            (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        elif self.options.browser == "firefox":
            (self.profile / "WebDriverBiDiServer.json").unlink(missing_ok=True)
            user_js = self.profile / "user.js"
            existing = user_js.read_text(encoding="utf-8") if user_js.exists() else ""
            kept = existing.split(_PREFS_MARKER)[0]
            user_js.write_text(kept + firefox_user_js(self.options), encoding="utf-8")

    def _read_endpoint(self) -> str | None:
        browser = self.options.browser
        if browser == "chromium":
            port_file = self.profile / "DevToolsActivePort"
            lines = port_file.read_text().split() if port_file.exists() else []
            return f"ws://127.0.0.1:{lines[0]}{lines[1]}" if len(lines) >= 2 else None
        if browser == "firefox":
            match = re.search(r"WebDriver BiDi listening on (ws://[^\s]+)", self._log_text())
            return f"{match.group(1)}/session" if match else None
        match = re.search(r"127\.0\.0\.1:(\d+)", self._log_text())
        return f"ws://127.0.0.1:{match.group(1)}/" if match else None

    def _log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    async def _wait_for_endpoint(self) -> str:
        deadline = time.perf_counter() + self.options.timeout
        while time.perf_counter() < deadline:
            if self._process.poll() is not None:
                raise LaunchError(
                    f"{self.options.browser} exited with code {self._process.returncode} before opening its endpoint. "
                    f"Log tail:\n{self._log_text()[-1500:]}"
                )
            endpoint = self._read_endpoint()
            if endpoint:
                return endpoint
            await asyncio.sleep(0.05)
        raise LaunchError(
            f"{self.options.browser} did not open its endpoint in {self.options.timeout}s. "
            f"Log tail:\n{self._log_text()[-1500:]}"
        )

    async def wait_and_stop(self, grace: float = 5.0) -> None:
        """For after a protocol-level "close browser": give it `grace` seconds to exit cleanly (profile data is
        flushed on the way out), then make sure nothing is left and reap the child."""
        deadline = time.perf_counter() + grace
        while self._process is not None and self._process.poll() is None and time.perf_counter() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.get_running_loop().run_in_executor(None, self.stop)

    def stop(self) -> None:
        """Synchronous stop (also the `atexit` safety net)."""
        if self.pid is not None:
            terminate_process_group(self.pid)
            self.pid = None
        if self._process is not None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def detach(self) -> None:
        """Forget the child without stopping it (named instances keep running)."""
        if self._process is not None:
            self._process.returncode = 0  # silences Popen's "still running" warning; the instance owns it now
        self._process = None
        self.pid = None
        self.run_dir = None
