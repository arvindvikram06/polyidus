from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

from pydantic import BaseModel

# `diff --git a/<path> b/<path>` — the `b/` (post-image) path is what
# `git diff --name-only` reports, including for renames, so it is the key we
# match scoping requests against.
_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.MULTILINE)


class DiffContext(BaseModel):
    diff_text: str
    changed_files: list[str]


@lru_cache(maxsize=1)
def _encoding():
    # Imported lazily: loading the BPE table is slow and pulls a network
    # dependency on first use, and the diff-splitting helpers below must stay
    # importable (and testable) without it.
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

    An empty ``files`` means "no scoping requested" and returns the whole diff.
    If none of the requested paths are present the full diff is returned too —
    a specialist reviewing everything is wasteful, but one reviewing nothing is
    useless.
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
