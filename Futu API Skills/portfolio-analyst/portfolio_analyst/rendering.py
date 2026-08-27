"""Control-safe terminal text and Markdown escaping for untrusted portfolio labels."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
_MARKDOWN = re.compile(r"([\\`*{}\[\]()#+|>])")


def terminal_text(value: Any) -> str:
    text = _ANSI.sub("", str(value))
    safe = "".join(
        " " if character.isspace() or unicodedata.category(character).startswith("C") else character
        for character in text
    )
    return " ".join(safe.split())


def markdown_text(value: Any) -> str:
    return _MARKDOWN.sub(r"\\\1", html.escape(terminal_text(value), quote=True))
