"""Rules every specialist shares, loaded once and composed into each prompt.

Kept out of the individual `prompts/*.md` files: the first real run reported one
hardcoded credential twice, `critical` by security and `high` by coding
standards, because nothing defined what those words meant. Four copies of a
rubric become four rubrics.

It lives in `prompts/_shared/` because `catalog.load_specialists()` globs
`prompts/*.md` to decide what the specialists *are*, and this is not one.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from reviewer.config import SUBAGENT_TOOL_ITERATION_CAP
from reviewer.sandbox.files import _MAX_GREP_MATCHES, _MAX_READ_LINES

_RULES_PATH = Path(__file__).resolve().parent.parent / "prompts" / "_shared" / "review_rules.md"


class RulesMissingError(RuntimeError):
    """The shared rules file was not found — almost certainly a packaging bug."""


@lru_cache(maxsize=1)
def shared_rules() -> str:
    """The shared rules text.

    Raises rather than defaulting: a specialist silently running without the
    rubric would surface as inconsistent severities on a real pull request,
    not as an error anyone notices.
    """
    if not _RULES_PATH.exists():
        raise RulesMissingError(
            f"shared review rules not found at {_RULES_PATH}. If this is an "
            "installed package, the file was not included in the wheel."
        )
    return _RULES_PATH.read_text(encoding="utf-8").strip()


def compose_system_prompt(
    specialist_prompt: str, tool_budget: int = SUBAGENT_TOOL_ITERATION_CAP
) -> str:
    """The specialist's own prompt, plus the rules every specialist obeys.

    Specialist first: its domain instructions are what this run is for, and the
    shared rules are the frame around them. The separator is explicit so the
    model can tell where one ends and the other begins.

    Every number the rules quote is substituted here rather than written into
    the markdown, so the figure the model is told is the figure the code
    enforces. A prose number drifts the moment anyone tunes the constant, and a
    model told it may read 2000 lines when it may read 500 spends the
    difference discovering that.
    """
    substitutions = {
        "{tool_budget}": str(tool_budget),
        "{read_limit}": str(_MAX_READ_LINES),
        "{grep_limit}": str(_MAX_GREP_MATCHES),
    }
    rules = shared_rules()
    for placeholder, value in substitutions.items():
        rules = rules.replace(placeholder, value)
    return f"{specialist_prompt.strip()}\n\n---\n\n{rules}"
