"""Map a finding onto a line GitHub will accept as a review comment.

GitHub's review API needs ``path`` + ``line`` + ``side`` and 422s any line not
in the diff. This is that translation, and the only place that understands
unified-diff line arithmetic.

A hunk header ``@@ -2,5 +2,7 @@`` means the old section starts at line 2 for 5
lines, the new at 2 for 7. Walking the body: context (' ') advances both
counters, an addition ('+') only the new, a deletion ('-') only the old. A line
is addressable on RIGHT if it exists in the new file, LEFT if in the old.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field

_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


class AnchorState(str, Enum):
    """How precisely a finding could be attached to the diff."""

    LINE = "line"  # inline comment on a specific line
    FILE = "file"  # the file changed, but not at the claimed line
    NONE = "none"  # the file is not in this diff at all


class FileHunks(BaseModel):
    """Every line of one file that a review comment may legally address."""

    path: str
    # New-file line numbers present in some hunk (additions + context).
    right: set[int] = Field(default_factory=set)
    # Old-file line numbers present in some hunk (deletions + context).
    left: set[int] = Field(default_factory=set)
    # Additions only — a comment on introduced code is almost always meant.
    added: set[int] = Field(default_factory=set)


class CommentAnchor(BaseModel):
    """A position GitHub will accept, or an explanation of why there isn't one."""

    state: AnchorState
    path: str
    line: int | None = None
    side: str | None = None
    start_line: int | None = None
    start_side: str | None = None

    def as_comment(self, body: str) -> dict:
        """Render as one element of the review API's ``comments`` array."""
        if self.state is AnchorState.NONE:
            raise ValueError("an unanchorable finding has no comment form")
        if self.state is AnchorState.FILE:
            return {"path": self.path, "body": body, "subject_type": "file"}
        comment = {"path": self.path, "body": body, "line": self.line, "side": self.side}
        if self.start_line is not None:
            comment["start_line"] = self.start_line
            comment["start_side"] = self.start_side or self.side
        return comment


def parse_hunks(diff_text: str) -> dict[str, FileHunks]:
    """Index every addressable line in a unified diff, keyed by post-image path."""
    files: dict[str, FileHunks] = {}
    current: FileHunks | None = None
    old_line = new_line = 0

    for raw in diff_text.splitlines():
        header = _FILE_HEADER.match(raw)
        if header:
            current = FileHunks(path=header.group("b"))
            files[current.path] = current
            old_line = new_line = 0
            continue

        if current is None:
            continue

        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            old_line = int(hunk.group("old_start"))
            new_line = int(hunk.group("new_start"))
            continue

        if not new_line and not old_line:
            continue  # still in the file header (index/---/+++ lines)

        if raw.startswith("+"):
            current.right.add(new_line)
            current.added.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            current.left.add(old_line)
            old_line += 1
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        elif raw.startswith(" ") or raw == "":
            current.right.add(new_line)
            current.left.add(old_line)
            new_line += 1
            old_line += 1
        # Anything else ends the hunk body (a new `diff --git` is caught above).

    return files


def anchor_finding(
    hunks: dict[str, FileHunks],
    file_path: str,
    line_range: tuple[int, int] | None,
) -> CommentAnchor:
    """Resolve one finding to the most precise position GitHub will accept.

    Falls back rather than failing: unplaceable becomes file-level, a file
    outside the diff becomes NONE for the caller to fold into the body. Nothing
    is silently dropped, nothing is posted at a line that would 422.
    """
    file_hunks = hunks.get(file_path)
    if file_hunks is None:
        return CommentAnchor(state=AnchorState.NONE, path=file_path)

    if line_range is None:
        return CommentAnchor(state=AnchorState.FILE, path=file_path)

    start, end = sorted(line_range)

    # Prefer the RIGHT side: findings are nearly always about introduced code.
    for side, addressable in (("RIGHT", file_hunks.right), ("LEFT", file_hunks.left)):
        if end not in addressable:
            continue
        anchor = CommentAnchor(state=AnchorState.LINE, path=file_path, line=end, side=side)
        # GitHub requires start_line strictly above line, on the same side.
        if start != end and start in addressable:
            anchor.start_line = start
            anchor.start_side = side
        return anchor

    return CommentAnchor(state=AnchorState.FILE, path=file_path)

def anchor_findings(findings: list, diff_text: str) -> None:
    """Resolve every finding to a position GitHub will accept, in place.

    Pure anchoring — the diff and the findings go in, `finding.anchor` comes out.

    Done before a human sees anything, so triage can show what will land inline,
    what only on the file, and what cannot be posted — and so a hallucinated line
    number is caught here rather than as a 422.
    """
    hunks = parse_hunks(diff_text)
    for finding in findings:
        finding.anchor = anchor_finding(hunks, finding.file_path, finding.line_range)
