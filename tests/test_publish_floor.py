"""What reaches a pull request as an inline comment, and what does not.

A review is read in severity order or not at all. One real run posted an
`info` finding whose own text said "this is an observation, not a defect
requiring immediate action" — as an inline comment, nine lines above an
unfixed SQL injection. Every comment spends the reader's attention, so the
ones that announce they need no action must not be there.
"""

from __future__ import annotations

import pytest

from bot.review.publish import MAX_INLINE_COMMENTS, build_comments
from reviewer.models.anchor import AnchorState, CommentAnchor
from reviewer.models.findings import Finding, Severity


def make(severity: Severity, title: str = "t") -> Finding:
    f = Finding(
        subagent="architecture", file_path="src/Repo.cs", offending_line="var x = 1;",
        line_range=(10, 10), severity=severity, title=title, message="m",
        verified_by="v", diff_context="",
    )
    f.anchor = CommentAnchor(state=AnchorState.LINE, path="src/Repo.cs", line=10, side="RIGHT")
    return f


def test_info_findings_are_not_posted_inline():
    line, file_, unplaceable, held = build_comments([make(Severity.INFO)])

    assert line == [] and file_ == [], "an `info` item must not take a comment slot"
    assert unplaceable == [], "it was placeable — it was withheld on purpose"
    assert held == 1, "and it must still be counted, not silently dropped"


@pytest.mark.parametrize(
    "severity", [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
)
def test_everything_from_low_upwards_is_posted(severity: Severity):
    line, _, _, held = build_comments([make(severity)])
    assert len(line) == 1 and held == 0


def test_the_cap_drops_the_least_important_first():
    findings = [make(Severity.LOW, f"low {i}") for i in range(MAX_INLINE_COMMENTS)]
    findings.append(make(Severity.CRITICAL, "the one that matters"))

    line, _, _, held = build_comments(findings)

    assert held == 1, "the cap bit"
    assert any("the one that matters" in c["body"] for c in line), \
        "severity sorting must run before the cap, or the cap drops the wrong one"
