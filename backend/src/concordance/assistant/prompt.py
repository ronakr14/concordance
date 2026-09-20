"""Rendering a question into the two turns that ask a model for SQL.

Versioned on disk like the adjudication prompt, for the same reason: the
version is recorded with every answer, so "why did it write that" stays
answerable, and editing the text means adding `nl_to_sql_v2.md`.

**This prompt carries attacker-controlled text and the adjudication one does
not.** A question is free text by definition, so the structural defence used
there - refuse to render anything that is not an identifier - is not available.
What is done instead: the question is delimited and the system prompt says that
everything inside is data; the question is capped and stripped of the delimiter
so it cannot close its own block; and nothing the model returns is trusted -
`guard.py` parses it and the read-only role bounds it. The prompt is the first
line, not the defence.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from concordance.assistant.views import schema_prompt
from concordance.llm.types import ChatMessage, system, user

#: Bumped by adding a new prompt file, never by editing an existing one.
PROMPT_VERSION = "nl_to_sql_v1"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: Longer than this is not a question. Cheap, and it bounds the prompt.
MAX_QUESTION_CHARS = 500

OPEN, CLOSE = "<<<QUESTION", "QUESTION>>>"
#: Control characters and the delimiter itself: a question may not close its own block.
_STRIP = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|<<<QUESTION|QUESTION>>>", re.IGNORECASE)


class QuestionRejectedError(ValueError):
    """The question was refused before any model saw it."""


@lru_cache(maxsize=8)
def load_prompt(version: str = PROMPT_VERSION) -> str:
    path = PROMPTS_DIR / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt file for version {version!r} at {path}")
    return path.read_text(encoding="utf-8").strip()


def clean(question: str) -> str:
    """The question as it will be rendered: trimmed, capped, delimiter-free."""
    text = _STRIP.sub(" ", (question or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise QuestionRejectedError("ask a question first")
    if len(text) > MAX_QUESTION_CHARS:
        raise QuestionRejectedError(
            f"that question is {len(text)} characters; keep it under {MAX_QUESTION_CHARS}"
        )
    return text


def render(question: str, version: str = PROMPT_VERSION) -> list[ChatMessage]:
    """The system turn with the schema, and the question inside its markers."""
    instructions = load_prompt(version).replace("{schema}", schema_prompt())
    return [system(instructions), user(f"{OPEN}\n{clean(question)}\n{CLOSE}")]


def extract_sql(answer: str) -> str:
    """The SQL out of whatever the model wrapped it in.

    Models fence code even when told not to, and some open with a sentence.
    Unwrapping is not sanitizing - whatever comes out still goes through the
    guard - it just avoids refusing an answer over a pair of backticks.
    """
    text = (answer or "").strip()
    fenced = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()
    if "\n" in text:
        # Drop any lead-in before the first SELECT or WITH.
        found = re.search(r"\b(SELECT|WITH)\b", text, re.I)
        if found:
            text = text[found.start() :].strip()
    return text.rstrip().rstrip(";").strip()


__all__ = [
    "MAX_QUESTION_CHARS",
    "PROMPT_VERSION",
    "QuestionRejectedError",
    "clean",
    "extract_sql",
    "load_prompt",
    "render",
]
