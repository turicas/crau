"""`pip install crau` brings no dependency; archiving needs `crau[archive]` and says so."""

import subprocess
import sys

BLOCK_THIRD_PARTY = """
import sys

class Blocker:
    blocked = ("h11", "lxml", "tqdm", "warcio", "websockets", "pywb")

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.blocked:
            raise ImportError(f"No module named {name!r} (blocked by the test)")

sys.meta_path.insert(0, Blocker())
"""


def run_without_dependencies(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", BLOCK_THIRD_PARTY + code], capture_output=True, text=True, timeout=60)


def test_package_imports_without_any_third_party_module():
    result = run_without_dependencies("import crau; print(crau.__version__)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_help_and_version_work_without_dependencies():
    result = run_without_dependencies("from crau.cli import main; main(['--help'])")
    assert result.returncode == 0 and "archive" in result.stdout, result.stderr


def test_archive_commands_explain_which_extra_is_missing():
    code = "import sys; from crau.cli import main; sys.exit(main(['list', 'x.warc.gz']))"
    result = run_without_dependencies(code)
    assert result.returncode == 1
    assert "pip install 'crau[archive]'" in result.stderr and "Traceback" not in result.stderr


def test_public_names_are_still_importable_from_the_package():
    import crau

    for name in ("Crawler", "Request", "Response", "Spider", "cli", "main", "__version__"):
        assert getattr(crau, name) is not None
    assert sorted(crau.__all__) == sorted(["Crawler", "Request", "Response", "Spider", "cli", "main", "__version__"])
