"""Loads the in-page JavaScript from `page.js` (kept as a real JavaScript file so editors and linters can read it)."""

from __future__ import annotations

import json
from pathlib import Path

_MARKER = "// == "


def load_page_functions(path: Path) -> dict[str, str]:
    """Split `page.js` on its `// == NAME` markers and inline the FIND helper where `/*FIND*/` appears."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(_MARKER):
            current = line[len(_MARKER) :].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    sources = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    find = sources.pop("FIND")
    return {name: source.replace("/*FIND*/", find) for name, source in sources.items()}


_FUNCTIONS = load_page_functions(Path(__file__).with_name("page.js"))
EXISTS = _FUNCTIONS["EXISTS"]
ELEMENT_BOX = _FUNCTIONS["ELEMENT_BOX"]
CLICK = _FUNCTIONS["CLICK"]
FOCUS = _FUNCTIONS["FOCUS"]
FILL = _FUNCTIONS["FILL"]
SCROLL_STEP = _FUNCTIONS["SCROLL_STEP"]
PAGE_SIZE = _FUNCTIONS["PAGE_SIZE"]
TEXT = _FUNCTIONS["TEXT"]
HTML = _FUNCTIONS["HTML"]
OUTLINE = _FUNCTIONS["OUTLINE"]


def call(function_source: str, *arguments: object) -> str:
    """Expression that calls a JS function with JSON-encoded arguments."""
    return f"({function_source})({', '.join(json.dumps(argument) for argument in arguments)})"
