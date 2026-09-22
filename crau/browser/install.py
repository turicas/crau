"""Install the browsers crau drives: chromium, firefox, lightpanda (GNU/Linux, typically inside a container).

The distribution is detected from /etc/os-release and each browser gets the install route that actually works there:

  Debian   chromium: apt           firefox: Mozilla APT repository (`--channel esr`: distro firefox-esr)
  Ubuntu   chromium: Chrome for Testing download (the apt package is a snap stub, useless in containers)
           firefox: Mozilla APT repository
  Alpine   chromium/firefox: apk   lightpanda: unsupported (glibc binary)
  other    Chrome for Testing, Mozilla tarball and the Lightpanda release binary, all into the user's home

Firefox defaults to the current release, not ESR: ESR 140 predates the BiDi commands used for response bodies in HAR
captures and for extra request headers.

"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

BROWSERS = ("chromium", "firefox", "lightpanda")
METHODS = ("auto", "system", "download")
CHANNELS = ("stable", "esr", "nightly")
CFT_VERSIONS = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
MOZILLA_KEY_URL = "https://packages.mozilla.org/apt/repo-signing-key.gpg"
MOZILLA_KEY_PATH = "/etc/apt/keyrings/packages.mozilla.org.asc"
MOZILLA_SOURCE = f"deb [signed-by={MOZILLA_KEY_PATH}] https://packages.mozilla.org/apt mozilla main\n"
MOZILLA_PIN = "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n"
FIREFOX_DOWNLOAD = "https://download.mozilla.org/?product={product}&os={os}&lang=en-US"
LIGHTPANDA_DOWNLOAD = "https://github.com/lightpanda-io/browser/releases/{release}/lightpanda-{arch}-linux"
# shared libraries the downloaded builds need; each tuple lists alternative package names (Ubuntu 24.04 renamed
# several with a t64 suffix) and the first one the package index knows is installed
CHROME_LIBRARIES = [
    ("libasound2t64", "libasound2"), ("libatk-bridge2.0-0t64", "libatk-bridge2.0-0"), ("libatk1.0-0t64", "libatk1.0-0"),
    ("libcups2t64", "libcups2"), ("libglib2.0-0t64", "libglib2.0-0"), ("libdbus-1-3",), ("libdrm2",), ("libgbm1",),
    ("libnspr4",), ("libnss3",), ("libpango-1.0-0",), ("libcairo2",), ("libx11-6",), ("libxcb1",), ("libxcomposite1",),
    ("libxdamage1",), ("libxext6",), ("libxfixes3",), ("libxkbcommon0",), ("libxrandr2",), ("fonts-liberation",),
    ("ca-certificates",),
]  # fmt: skip
FIREFOX_LIBRARIES = [
    ("libgtk-3-0t64", "libgtk-3-0"), ("libasound2t64", "libasound2"), ("libdbus-glib-1-2",), ("libx11-xcb1",),
    ("libxtst6",), ("fonts-liberation",), ("ca-certificates",), ("xz-utils",),
]  # fmt: skip


class InstallError(Exception):
    """Something went wrong while installing (exit code 1)."""


class UnsupportedSystem(InstallError):
    """This browser cannot be installed on this system (exit code 3)."""


@dataclass
class System:
    family: str  # debian, ubuntu, alpine or other
    arch: str  # x86_64 or aarch64
    root: bool
    sudo: bool = False

    @property
    def can_use_packages(self) -> bool:
        return self.family != "other" and (self.root or self.sudo)


@dataclass
class Step:
    """One unit of the plan. `kind`: run, write, download-file, download-archive, apt-libraries, link."""

    kind: str
    description: str
    command: list[str] = field(default_factory=list)
    url: str = ""
    path: str = ""
    content: str = ""
    packages: list[tuple[str, ...]] = field(default_factory=list)


def parse_os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator and not name.startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    return values


def detect_family(os_release: dict[str, str]) -> str:
    identifiers = [os_release.get("ID", "").lower(), *os_release.get("ID_LIKE", "").lower().split()]
    for family in ("ubuntu", "debian", "alpine"):  # Ubuntu first: its ID_LIKE also says debian
        if family in identifiers:
            return family
    return "other"


def detect_system() -> System:
    path = Path("/etc/os-release")
    os_release = parse_os_release(path.read_text(encoding="utf-8")) if path.exists() else {}
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return System(detect_family(os_release), arch, os.geteuid() == 0, shutil.which("sudo") is not None)


def data_dir(system: System) -> Path:
    if system.root:
        return Path("/opt/crau")
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local" / "share"
    return base / "crau"


def bin_dir(system: System) -> Path:
    return Path("/usr/local/bin") if system.root else Path.home() / ".local" / "bin"


def _apt(*packages: str) -> list[Step]:
    install = ["apt-get", "install", "-y", "--no-install-recommends", *packages]
    return [
        Step("run", "Refresh the package index", command=["apt-get", "update"]),
        Step("run", f"Install {', '.join(packages)}", command=install),
    ]


def _mozilla_repository() -> list[Step]:
    return [
        *_apt("ca-certificates"),
        Step("download-file", "Fetch the Mozilla APT signing key", url=MOZILLA_KEY_URL, path=MOZILLA_KEY_PATH),
        Step(
            "write",
            "Add the Mozilla APT repository",
            path="/etc/apt/sources.list.d/mozilla.list",
            content=MOZILLA_SOURCE,
        ),
        Step("write", "Prefer packages from Mozilla", path="/etc/apt/preferences.d/mozilla", content=MOZILLA_PIN),
    ]


def plan(browser: str, system: System, method: str = "auto", channel: str = "stable") -> list[Step]:
    """Steps to install `browser` on `system`. Pure function: inspects nothing, changes nothing."""
    if system.arch not in ("x86_64", "aarch64"):
        raise UnsupportedSystem(f"No {browser} build for {system.arch}")
    if channel == "esr" and browser != "firefox":
        raise InstallError("--channel esr only applies to firefox")
    if channel == "nightly" and browser != "lightpanda":
        raise InstallError("--channel nightly only applies to lightpanda")
    use_packages = method == "system" or (method == "auto" and system.can_use_packages)
    if method == "system" and not system.can_use_packages:
        raise InstallError("--method system needs root (or sudo) on Debian, Ubuntu or Alpine")
    target, links = data_dir(system), bin_dir(system)
    libraries = system.family in ("debian", "ubuntu") and (system.root or system.sudo)

    if browser == "lightpanda":
        if system.family == "alpine":
            raise UnsupportedSystem("Lightpanda ships a glibc binary; it does not run on Alpine (musl)")
        release = "download/nightly" if channel == "nightly" else "latest/download"
        url = LIGHTPANDA_DOWNLOAD.format(release=release, arch=system.arch)
        return [Step("download-file", "Download the Lightpanda binary", url=url, path=str(links / "lightpanda"))]

    if browser == "chromium":
        if use_packages and system.family == "debian":
            return _apt("chromium")
        if use_packages and system.family == "alpine":
            return [Step("run", "Install chromium", command=["apk", "add", "--no-cache", "chromium"])]
        if system.family == "alpine":
            raise UnsupportedSystem("Chrome for Testing is a glibc build; on Alpine use --method system as root")
        steps = []
        if libraries:
            steps.append(Step("apt-libraries", "Install the shared libraries Chrome needs", packages=CHROME_LIBRARIES))
        platform_name = "linux64" if system.arch == "x86_64" else "linux-arm64"
        steps.append(
            Step(
                "download-archive", "Download Chrome for Testing (stable)", url=f"cft:{platform_name}", path=str(target)
            )
        )
        chrome = target / f"chrome-{platform_name}" / "chrome"
        steps.append(Step("link", "Link it as 'chromium'", path=str(links / "chromium"), content=str(chrome)))
        return steps

    package = "firefox-esr" if channel == "esr" else "firefox"
    if use_packages and system.family == "alpine":
        return [Step("run", f"Install {package}", command=["apk", "add", "--no-cache", package])]
    if use_packages and system.family == "debian" and channel == "esr":
        return _apt("firefox-esr")
    if use_packages and system.family in ("debian", "ubuntu"):
        return [*_mozilla_repository(), *_apt(package)]
    if system.family == "alpine":
        raise UnsupportedSystem("Mozilla's tarball is a glibc build; on Alpine use --method system as root")
    steps = []
    if libraries:
        steps.append(Step("apt-libraries", "Install the shared libraries Firefox needs", packages=FIREFOX_LIBRARIES))
    product = "firefox-esr-latest-ssl" if channel == "esr" else "firefox-latest-ssl"
    url = FIREFOX_DOWNLOAD.format(product=product, os="linux64" if system.arch == "x86_64" else "linux64-aarch64")
    steps.append(Step("download-archive", f"Download Firefox ({channel})", url=url, path=str(target)))
    steps.append(
        Step("link", "Link it as 'firefox'", path=str(links / "firefox"), content=str(target / "firefox" / "firefox"))
    )
    return steps


def describe(step: Step) -> str:
    if step.kind == "run":
        return f"{step.description}: {' '.join(step.command)}"
    if step.kind == "write":
        return f"{step.description}: write {step.path}"
    if step.kind == "link":
        return f"{step.description}: {step.path} -> {step.content}"
    if step.kind == "apt-libraries":
        return f"{step.description}: apt-get install {' '.join(names[0] for names in step.packages)}"
    return f"{step.description}: {step.url} -> {step.path}"


# --- execution ----------------------------------------------------------------------------------------------------


class Executor:
    def __init__(self, system: System, verbose: bool = False, quiet: bool = False) -> None:
        self.system, self.verbose, self.quiet = system, verbose, quiet

    def log(self, message: str) -> None:
        if not self.quiet:
            print(message, file=sys.stderr, flush=True)

    def _privileged(self, command: list[str]) -> list[str]:
        return command if self.system.root else ["sudo", *command]

    def _run(self, command: list[str], timeout: int = 1800) -> None:
        environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        try:
            result = subprocess.run(
                self._privileged(command), capture_output=not self.verbose, text=True, timeout=timeout, env=environment
            )
        except subprocess.TimeoutExpired:
            raise InstallError(f"{' '.join(command)} did not finish in {timeout}s") from None
        if result.returncode != 0:
            details = "" if self.verbose else f"\n{(result.stderr or result.stdout or '').strip()[-2000:]}"
            raise InstallError(f"{' '.join(command)} failed with code {result.returncode}{details}")

    def _download(self, url: str, destination: Path) -> None:
        """Stream to a temporary file next to the destination and move it into place only when complete."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "crau-install"})
        handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".download-")
        try:
            with os.fdopen(handle, "wb") as output, urllib.request.urlopen(request, timeout=60) as response:
                total, received, last = int(response.headers.get("Content-Length") or 0), 0, 0.0
                while chunk := response.read(1 << 20):
                    output.write(chunk)
                    received += len(chunk)
                    if self.verbose and time.perf_counter() - last > 1:
                        last = time.perf_counter()
                        self.log(f"  {received >> 20} MiB" + (f" of {total >> 20} MiB" if total else ""))
            os.replace(temporary, destination)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _write_privileged(self, path: Path, content: bytes) -> None:
        if self.system.root:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return
        with tempfile.NamedTemporaryFile(delete=False) as temporary:
            temporary.write(content)
        try:
            self._run(["install", "-D", "-m", "0644", temporary.name, str(path)])
        finally:
            Path(temporary.name).unlink(missing_ok=True)

    def _chrome_for_testing_url(self, platform_name: str) -> str:
        with urllib.request.urlopen(CFT_VERSIONS, timeout=60) as response:
            downloads = json.load(response)["channels"]["Stable"]["downloads"]["chrome"]
        for item in downloads:
            if item["platform"] == platform_name:
                return item["url"]
        raise UnsupportedSystem(f"Chrome for Testing has no {platform_name} build")

    def _resolve_libraries(self, packages: list[tuple[str, ...]]) -> list[str]:
        chosen = []
        for alternatives in packages:
            for name in alternatives:
                known = subprocess.run(["apt-cache", "show", name], capture_output=True, text=True, timeout=60)
                if known.returncode == 0 and known.stdout.strip():
                    chosen.append(name)
                    break
        return chosen

    def execute(self, step: Step) -> None:
        self.log(f"- {describe(step)}")
        if step.kind == "run":
            self._run(step.command)
        elif step.kind == "write":
            self._write_privileged(Path(step.path), step.content.encode("utf-8"))
        elif step.kind == "apt-libraries":
            self._run(["apt-get", "update"])
            self._run(["apt-get", "install", "-y", "--no-install-recommends", *self._resolve_libraries(step.packages)])
        elif step.kind == "download-file":
            destination = Path(step.path)
            if destination.is_relative_to("/etc"):
                with tempfile.TemporaryDirectory() as directory:
                    self._download(step.url, Path(directory) / "file")
                    self._write_privileged(destination, (Path(directory) / "file").read_bytes())
            else:
                self._download(step.url, destination)
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        elif step.kind == "download-archive":
            url = self._chrome_for_testing_url(step.url[4:]) if step.url.startswith("cft:") else step.url
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "archive"
                self._download(url, archive)
                extract_archive(archive, Path(step.path))
        elif step.kind == "link":
            link, target = Path(step.path), Path(step.content)
            if not target.is_file():
                raise InstallError(f"Expected {target} after extraction, but it is not there")
            link.parent.mkdir(parents=True, exist_ok=True)
            link.unlink(missing_ok=True)
            link.symlink_to(target)


def _safe_target(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise InstallError(f"Archive entry escapes the destination: {name}")
    return target


def extract_archive(archive: Path, destination: Path) -> None:
    """Extract a .zip or .tar.* refusing entries that would land outside `destination`."""
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = _safe_target(destination, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = info.external_attr >> 16
                if mode:
                    target.chmod(mode & 0o777)
        return
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            _safe_target(destination, member.name)
            if member.issym() or member.islnk():
                _safe_target(destination, str(Path(member.name).parent / member.linkname))
        if hasattr(tarfile, "data_filter"):
            bundle.extractall(destination, filter="data")
        else:
            bundle.extractall(destination)


def installed_binary(browser: str, system: System) -> Path | None:
    names = {"chromium": ("chromium", "chromium-browser"), "firefox": ("firefox", "firefox-esr")}.get(
        browser, (browser,)
    )
    for name in names:
        found = shutil.which(name) or (str(bin_dir(system) / name) if (bin_dir(system) / name).exists() else None)
        if found:
            return Path(found)
    return None


def binary_version(binary: Path) -> str:
    argument = "version" if binary.name == "lightpanda" else "--version"
    environment = {**os.environ, "LIGHTPANDA_DISABLE_TELEMETRY": "true"}
    if environment.get("HOME", "/") == "/":
        environment["HOME"] = tempfile.gettempdir()
    result = subprocess.run([str(binary), argument], capture_output=True, text=True, timeout=60, env=environment)
    lines = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not lines:
        raise InstallError(f"{binary} {argument} failed: {(result.stderr or result.stdout).strip()[-500:]}")
    return lines[-1].strip()


# --- command line (`crau browser install`) ---------------------------------------------------------------------------


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "browser", nargs="+", choices=(*BROWSERS, "all"), metavar="browser",
        help=f"Browser(s) to install. Options: {', '.join(BROWSERS)}, all",
    )  # fmt: skip
    parser.add_argument(
        "-m", "--method", metavar="method", choices=METHODS, default="auto",
        help=f"system = distribution packages, download = official builds into the home directory. "
        f"Options: {', '.join(METHODS)} (default: auto, packages when root or sudo is available)",
    )  # fmt: skip
    parser.add_argument(
        "-c", "--channel", metavar="channel", choices=CHANNELS, default="stable",
        help=f"esr applies to firefox, nightly to lightpanda. Options: {', '.join(CHANNELS)} (default: stable)",
    )  # fmt: skip
    parser.add_argument("-n", "--dry-run", action="store_true", help="Print the steps and change nothing")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--quiet", dest="browser_quiet", action="store_true", help="Do not print the steps")
    group.add_argument(
        "-v", "--verbose", dest="browser_verbose", action="store_true",
        help="Show package manager output and download progress",
    )  # fmt: skip


def install(browsers: list[str], system: System, method: str, channel: str, dry_run: bool, executor: Executor) -> int:
    for browser in browsers:
        browser_channel = channel if (channel, browser) in (("esr", "firefox"), ("nightly", "lightpanda")) else "stable"
        steps = plan(browser, system, method, browser_channel)
        if dry_run:
            print(f"# {browser} ({system.family}, {system.arch}, {'root' if system.root else 'user'})")
            for step in steps:
                print(describe(step))
            continue
        for step in steps:
            executor.execute(step)
        binary = installed_binary(browser, system)
        if binary is None:
            raise InstallError(f"{browser} was installed but no binary was found on PATH or in {bin_dir(system)}")
        print(f"{browser}\t{binary}\t{binary_version(binary)}")
    return 0


def run(args: argparse.Namespace) -> int:
    """Exit codes: 0 success, 1 installation failed, 3 not supported on this system, 130 interrupted."""
    browsers = list(BROWSERS) if "all" in args.browser else list(dict.fromkeys(args.browser))
    system = detect_system()
    executor = Executor(system, args.browser_verbose, args.browser_quiet)
    try:
        return install(browsers, system, args.method, args.channel, args.dry_run, executor)
    except KeyboardInterrupt:
        print("\ncrau: interrupted", file=sys.stderr)
        return 130
    except UnsupportedSystem as exc:
        print(f"crau: error: {exc}", file=sys.stderr)
        return 3
    except (InstallError, OSError) as exc:
        print(f"crau: error: {exc}", file=sys.stderr)
        return 1
