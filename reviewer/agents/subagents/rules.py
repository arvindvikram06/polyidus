"""Rules every specialist shares, loaded once and composed into each prompt.

Kept out of the individual `prompts/*.md` files deliberately. The first real
run against a pull request produced the same problem — credentials hardcoded
and written to a log — reported twice, as `critical` by the security specialist
and `high` by the coding-standards one, because nothing anywhere defined what
those words meant. Four copies of a rubric become four rubrics; one copy,
injected, cannot.

The file lives in `prompts/_shared/` rather than `prompts/` because
`catalog.load_specialists()` globs `prompts/*.md` to decide what the
specialists *are*, and this is not one of them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_RULES_PATH = Path(__file__).resolve().parent.parent / "prompts" / "_shared" / "review_rules.md"


class RulesMissingError(RuntimeError):
    """The shared rules file was not found — almost certainly a packaging bug."""


@lru_cache(maxsize=1)
def shared_rules() -> str:
    """The shared rules text.

    Raises rather than returning a default. A specialist silently running
    without the severity rubric is the exact failure this module exists to
    prevent, and it would show up as inconsistent severities on a real pull
    request rather than as an error anyone would notice.
    """
    if not _RULES_PATH.exists():
        raise RulesMissingError(
            f"shared review rules not found at {_RULES_PATH}. If this is an "
            "installed package, the file was not included in the wheel."
        )
    return _RULES_PATH.read_text(encoding="utf-8").strip()


def compose_system_prompt(specialist_prompt: str) -> str:
    """The specialist's own prompt, plus the rules every specialist obeys.

    Specialist first: its domain instructions are what this run is for, and the
    shared rules are the frame around them. The separator is explicit so the
    model can tell where one ends and the other begins.
    """
    return (
        f"{specialist_prompt.strip()}\n\n"
        "---\n\n"
        f"{shared_rules()}"
    )
