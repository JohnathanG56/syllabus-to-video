"""Prompt templates live in prompts/*.md.

A file holds the system prompt, then a line ``---USER---``, then the user
message template. Placeholders are ``{{name}}`` (not str.format, because the
prompts contain Python code with braces).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
SPLIT = "---USER---"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> tuple[str, str]:
    text = (PROMPT_DIR / f"{name}.md").read_text()
    if SPLIT not in text:
        raise ValueError(f"prompt {name}.md is missing the {SPLIT} separator")
    system, user = text.split(SPLIT, 1)
    return system.strip(), user.strip()


def render(template: str, **values: object) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"missing prompt variable {key!r}")
        return str(values[key])
    return _PLACEHOLDER.sub(sub, template)


def build(name: str, **values: object) -> tuple[str, str]:
    system, user = load_prompt(name)
    return render(system, **values), render(user, **values)
