from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from crau.browser import install

DEBIAN = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_CODENAME=trixie\n'
UBUNTU = "ID=ubuntu\nID_LIKE=debian\nVERSION_ID='24.04'\n"
ALPINE = "ID=alpine\nVERSION_ID=3.22.1\n"
MINT = 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'
FAMILIES = [
    (DEBIAN, "debian"),
    (UBUNTU, "ubuntu"),
    (ALPINE, "alpine"),
    (MINT, "ubuntu"),
    ("ID=fedora\n", "other"),
    ("", "other"),
]


def kinds(steps) -> list[str]:
    return [step.kind for step in steps]


def commands(steps) -> list[str]:
    return [" ".join(step.command) for step in steps if step.command]


@pytest.mark.parametrize("text, family", FAMILIES)
def test_distribution_family_detection(text, family):
    assert install.detect_family(install.parse_os_release(text)) == family


def test_debian_root_uses_apt_for_chromium_and_mozilla_repository_for_firefox():
    system = install.System("debian", "x86_64", root=True)
    assert commands(install.plan("chromium", system))[-1] == "apt-get install -y --no-install-recommends chromium"
    firefox = install.plan("firefox", system)
    sources = [step for step in firefox if step.path == "/etc/apt/sources.list.d/mozilla.list"]
    assert sources and "packages.mozilla.org" in sources[0].content
    assert commands(firefox)[-1].endswith(" firefox")
    esr = install.plan("firefox", system, channel="esr")
    assert commands(esr)[-1].endswith(" firefox-esr") and not any("mozilla" in step.path for step in esr)


def test_ubuntu_never_uses_the_snap_stub_for_chromium():
    steps = install.plan("chromium", install.System("ubuntu", "x86_64", root=True))
    assert kinds(steps) == ["apt-libraries", "download-archive", "link"]
    assert steps[1].url == "cft:linux64" and steps[2].path == "/usr/local/bin/chromium"
    assert not any("apt-get install" in command and "chromium" in command for command in commands(steps))
    assert commands(install.plan("firefox", install.System("ubuntu", "x86_64", root=True)))[-1].endswith(" firefox")


def test_alpine_uses_apk_and_refuses_lightpanda_and_glibc_downloads():
    system = install.System("alpine", "x86_64", root=True)
    assert commands(install.plan("chromium", system)) == ["apk add --no-cache chromium"]
    assert commands(install.plan("firefox", system, channel="esr")) == ["apk add --no-cache firefox-esr"]
    with pytest.raises(install.UnsupportedSystem, match="glibc"):
        install.plan("lightpanda", system)
    with pytest.raises(install.UnsupportedSystem):
        install.plan("chromium", install.System("alpine", "x86_64", root=False), method="download")


def test_unprivileged_user_gets_downloads_into_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    system = install.System("debian", "aarch64", root=False, sudo=False)
    chromium = install.plan("chromium", system)
    assert kinds(chromium) == ["download-archive", "link"] and chromium[0].url == "cft:linux-arm64"
    assert chromium[1].path == str(tmp_path / ".local/bin/chromium")
    firefox = install.plan("firefox", system)
    assert "os=linux64-aarch64" in firefox[0].url and firefox[0].path == str(tmp_path / ".local/share/crau")
    lightpanda = install.plan("lightpanda", system, channel="nightly")
    assert lightpanda[0].url.endswith("/download/nightly/lightpanda-aarch64-linux")
    with pytest.raises(install.InstallError, match="needs root"):
        install.plan("chromium", system, method="system")


def test_invalid_channel_and_architecture_combinations():
    system = install.System("debian", "x86_64", root=True)
    with pytest.raises(install.InstallError, match="esr"):
        install.plan("chromium", system, channel="esr")
    with pytest.raises(install.InstallError, match="nightly"):
        install.plan("firefox", system, channel="nightly")
    with pytest.raises(install.UnsupportedSystem, match="riscv64"):
        install.plan("chromium", install.System("debian", "riscv64", root=True))


def test_archives_are_extracted_with_modes_and_traversal_is_refused(tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as bundle:
        info = zipfile.ZipInfo("chrome-linux64/chrome")
        info.external_attr = 0o755 << 16
        bundle.writestr(info, "#!/bin/sh\n")
    install.extract_archive(good, tmp_path / "out")
    extracted = tmp_path / "out/chrome-linux64/chrome"
    assert extracted.read_text() == "#!/bin/sh\n" and extracted.stat().st_mode & 0o111

    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as bundle:
        bundle.writestr("../escaped.txt", "x")
    evil_tar = tmp_path / "evil.tar"
    with tarfile.open(evil_tar, "w") as bundle:
        member = tarfile.TarInfo("../../escaped.txt")
        member.size = 1
        bundle.addfile(member, io.BytesIO(b"x"))
    for archive in (evil_zip, evil_tar):
        with pytest.raises(install.InstallError, match="escapes"):
            install.extract_archive(archive, tmp_path / "jail")
    assert not (tmp_path / "escaped.txt").exists()
