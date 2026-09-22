"""Keep the agent skill honest: `crau skill` prints it, files it points to exist, commands it shows parse."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from crau.cli import create_parser, main

ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "crau" / "skill"
DOCUMENTS = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]
REFERENCES = ["archiving", "bidi", "cdp", "library", "lightpanda", "network-capture", "pitfalls"]


def test_skill_frontmatter_has_name_and_a_trigger_rich_description():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---")[1]
    assert re.search(r"^name: crau$", frontmatter, re.M)
    description = re.search(r"^description: (.+)$", frontmatter, re.M).group(1)
    assert 200 < len(description) < 1024 and "Use this skill" in description
    assert len(text.splitlines()) < 200, "SKILL.md must stay short: details belong in references/"


def test_skill_command_prints_the_skill_and_its_references(capsys):
    assert main(["skill"]) == 0
    assert capsys.readouterr().out == (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert main(["skill", "--list"]) == 0
    assert capsys.readouterr().out.split() == REFERENCES
    for name in REFERENCES:
        assert main(["skill", "-r", name]) == 0
        assert capsys.readouterr().out == (SKILL_DIR / "references" / f"{name}.md").read_text(encoding="utf-8")
    assert main(["skill", "-r", "cdp.md"]) == 0  # the extension is optional
    capsys.readouterr()
    assert main(["skill", "-r", "nope"]) == 2
    assert "archiving" in capsys.readouterr().err


def test_every_reference_is_announced_in_the_skill():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for name in REFERENCES:
        assert f"- `{name}` - " in text


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_paths_mentioned_in_the_docs_exist(document):
    text = document.read_text(encoding="utf-8")
    for match in re.finditer(r"`((?:crau|examples|tests)/[\w./-]+\.(?:py|md|js))`", text):
        assert (ROOT / match.group(1)).exists(), f"{document.name} mentions {match.group(1)}"
    for match in re.finditer(r"`(browser_\w+\.py)`", text):
        assert (ROOT / "examples" / match.group(1)).exists(), f"{document.name} mentions {match.group(1)}"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_prose_wraps_at_120_columns(document):
    for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
        exempt = line.startswith(("|", "description:")) or "http" in line
        assert exempt or len(line) <= 120, f"{document.name}:{number} has {len(line)} columns"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_crau_commands_shown_in_the_docs_are_accepted_by_the_parser(document, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("out.har", "site.warc.gz", "urls.txt"):
        (tmp_path / name).write_text("{}")  # some commands check that their input exists while parsing
    parser = create_parser()
    text = document.read_text(encoding="utf-8")
    commands = re.findall(r"^crau ((?:-q )?(?:page|har|tab|browser|search|skill|archive|list|extract|play|pack) .+?)(?:\s{2,}#.*)?$", text, re.M)
    if document.name == "SKILL.md":
        assert len(commands) >= 15
    for command in commands:
        arguments = shlex.split(command.replace("[URL...]", "").replace("~", "/home/u").replace("...", ""))
        parser.parse_args(arguments)
