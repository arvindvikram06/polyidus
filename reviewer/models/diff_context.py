from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

from pydantic import BaseModel

# The `b/` (post-image) path is what `git diff --name-only` reports, renames
# included, so it is the key scoping requests match against.
_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.MULTILINE)


class DiffContext(BaseModel):
    diff_text: str
    changed_files: list[str]


@lru_cache(maxsize=1)
def _encoding():
    # Lazy: the BPE table is slow and fetches on first use, and the helpers
    # below must stay importable without it.
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def split_by_file(diff_text: str) -> dict[str, str]:
    """Split a unified diff into ``{path: that file's diff section}``.

    Each section runs from its own ``diff --git`` header up to the next one, so
    the pieces concatenate back into a valid diff.
    """
    headers = list(_FILE_HEADER.finditer(diff_text))
    if not headers:
        return {}

    sections: dict[str, str] = {}
    for index, header in enumerate(headers):
        start = header.start()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(diff_text)
        sections[header.group("b")] = diff_text[start:end]
    return sections


def slice_diff(diff_text: str, files: Sequence[str]) -> str:
    """Return only the sections of ``diff_text`` belonging to ``files``.

    Empty ``files`` means no scoping, and returns everything. So does a set of
    paths none of which are present — a specialist reviewing everything is
    wasteful, one reviewing nothing is useless.
    """
    if not files:
        return diff_text

    sections = split_by_file(diff_text)
    if not sections:
        return diff_text

    picked = [sections[path] for path in files if path in sections]
    if not picked:
        return diff_text
    return "".join(picked)
