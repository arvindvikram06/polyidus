"""Turn a finished review into inline comments on the pull request.

No model runs here: findings are already written, anchored and judged. One rule
shapes the module — every finding goes inline on the line it concerns, and the
summary comment contains no findings at all.

An inline comment creates a resolvable thread, and that thread *is* the record:
the finding, the reply, and the decision. A list in one comment carries none of
those and would need a database to track what was already said.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from bot.github import client
from reviewer.models.anchor import AnchorState
from reviewer.models.findings import Finding, Severity

log = logging.getLogger("bot.review.publish")

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}

# A review with thirty inline comments is dismissed, not read. The cap is on
# what is *posted*, not on what is found.
MAX_INLINE_COMMENTS = 10

# `info` is both "unverified" and "observation". One review posted "not a defect
# requiring immediate action" nine lines above an unfixed SQL injection — an item
# that announces it needs no action competes with the ones that do.
MIN_INLINE_SEVERITY = Severity.LOW


# An invisible fingerprint per comment, so a later run recognises what it already
# said — record and key in the same object, rather than a table that could
# disagree with what the human can see.
#
# The line number is deliberately NOT in the key: a push shifts every line below
# an edit, and re-posting everything after a push is the failure this prevents.
# Hashing the message alone collided on two hardcoded credentials in one file, so
# the file and title are both in the key.
_MARKER = re.compile(r"<!-- polyidus:([0-9a-f]{12}) -->")
_STRIP = re.compile(r"[`*_\s]+")


def marker_for(file_path: str, title: str) -> str:
    key = f"{file_path}|{_STRIP.sub(' ', title).strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def markers_in(body: str) -> set[str]:
    return set(_MARKER.findall(body or ""))


def comment_body(finding: Finding, also_found_by: list[str] | None = None) -> str:
    """One inline comment: the issue, its impact, and where the fix goes.

    Short on purpose — GitHub collapses a comment that needs scrolling.
    """
    lines = [f"**{finding.severity.value.upper()} — {finding.title}**", "", finding.message]
    if finding.suggested_patch:
        lines += ["", "```suggestion", finding.suggested_patch.rstrip(), "```"]

    credit = finding.subagent
    if also_found_by:
        # Independent agreement is evidence, so it is shown rather than thrown
        # away with the duplicate.
        credit += ", " + ", ".join(also_found_by)
    lines += ["", f"<sub>{credit}</sub>"]
    # Renders as nothing. Read back by the next run to skip what is already said.
    lines.append(f"<!-- polyidus:{marker_for(finding.file_path, finding.title)} -->")
    return "\n".join(lines)


def summary_body(
    *,
    head_sha: str,
    changed_files: int,
    posted: int,
    requested_by: str,
    unplaceable: list[Finding],
    held_back: int,
    already_said: int = 0,
    resolved: int = 0,
    marked_fixed: int = 0,
    overview: str = "",
    aborted: str | None = None,
    failed_specialists: list[str] | None = None,
) -> str:
    """The conversation-box comment. Context only — no findings.

    One exception: ``unplaceable`` findings concern files the PR does not touch,
    so they have no line. Dropping a real finding to satisfy a formatting rule is
    the wrong trade, so they go in a collapsed block.
    """
    # A clean review has to SAY it is clean — "0 inline comment(s) posted"
    # followed by a pointer to inline comments reads like a malfunction.
    settled = resolved + marked_fixed
    clean = posted == 0 and not unplaceable and not aborted and not failed_specialists
    if clean and not already_said:
        headline = (
            f"**No issues found** at `{head_sha[:8]}` — {changed_files} file(s) "
            "reviewed, nothing worth flagging."
        )
    elif clean:
        headline = (
            f"**Nothing new** at `{head_sha[:8]}` — {changed_files} file(s) reviewed. "
            f"{already_said} open finding(s) still stand; no new ones."
        )
    elif posted == 0 and settled:
        headline = (
            f"**All clear** at `{head_sha[:8]}` — {changed_files} file(s) reviewed, "
            f"and {settled} earlier finding(s) no longer come up."
        )
    else:
        headline = (
            f"**Review complete** at `{head_sha[:8]}` — {changed_files} file(s) changed, "
            f"{posted} inline comment(s) posted."
        )
    lines = [headline, ""]

    if aborted:
        lines += [f"> ⚠️ **Partial review.** {aborted}", ""]

    if failed_specialists:
        # First, because a clean review with no warning reads as fully examined.
        lines += [
            "> ⚠️ **Incomplete.** No usable output from "
            + ", ".join(f"`{s}`" for s in failed_specialists)
            + " — whatever those specialists cover was not reviewed.",
            "",
        ]

    if overview:
        # Context, not a finding. The rule it must not break is listing issues.
        lines += [overview, ""]

    if posted or unplaceable:
        lines += [
            (
                "Findings are inline on the lines they concern. Reply in a thread to "
                "disagree; resolve it when it is handled."
            ),
            "",
        ]

    if already_said:
        lines += [
            (
                f"<sub>{already_said} finding(s) already have an open thread and "
                "were not repeated.</sub>"
            ),
            "",
        ]
    if resolved:
        lines += [
            (
                f"<sub>✅ {resolved} thread(s) resolved — that code no longer reports "
                "a problem.</sub>"
            ),
            "",
        ]
    if marked_fixed:
        lines += [
            (
                f"<sub>✅ {marked_fixed} finding(s) look fixed — each thread has a "
                "reply saying so. Resolve them when you are happy.</sub>"
            ),
            "",
        ]

    if unplaceable:
        lines += [
            (
                f"<details><summary>{len(unplaceable)} finding(s) with no line in "
                "this diff</summary>"
            ),
            "",
            (
                "These concern code the pull request does not touch, so they could "
                "not be attached inline:"
            ),
            "",
        ]
        for f in unplaceable:
            lines.append(
                f"- **{f.severity.value.upper()}** `{f.file_path}` — {f.title}: {f.message}"
            )
        lines += ["", "</details>", ""]

    if held_back:
        lines += [
            (
                f"<sub>{held_back} lower-severity finding(s) not posted — below "
                f"`{MIN_INLINE_SEVERITY.value}`, or beyond the "
                f"{MAX_INLINE_COMMENTS}-comment cap.</sub>"
            ),
            "",
        ]

    lines.append(f"<sub>requested by @{requested_by}</sub>")
    return "\n".join(lines)


def split_already_said(
    findings: list[Finding], existing_bodies: list[str]
) -> tuple[list[Finding], list[Finding]]:
    """Separate findings we have already commented on from genuinely new ones.

    Without it, a re-run posts a second copy of every unfixed finding — measured:
    eleven comments, several restating a thread open two lines away.
    """
    on_pr: set[str] = set()
    for body in existing_bodies:
        on_pr |= markers_in(body)

    new: list[Finding] = []
    already: list[Finding] = []
    for finding in findings:
        if marker_for(finding.file_path, finding.title) in on_pr:
            already.append(finding)
        else:
            new.append(finding)
    return new, already


def build_comments(
    findings: list[Finding], agreed_by: dict[Any, list[str]] | None = None
) -> tuple[list[dict], list[dict], list[Finding], int]:
    """Split findings by how GitHub will accept them.

    Returns ``(line_comments, file_comments, unplaceable, held_back)``:

    * ``line_comments`` — anchored to a line; these go inside the review
    * ``file_comments`` — anchored to a file only; these CANNOT go inside a
      review and must be posted one at a time (see below)
    * ``unplaceable``   — no anchor at all; they belong in the summary
    * ``held_back``     — count dropped by the severity floor or density cap

    The line/file split is forced: a batched review's comments are
    ``DraftPullRequestReviewComment`` objects with no ``subject_type``, so one
    file-level comment in the array makes GitHub 422 the **entire** review.

    Sorted by severity so the cap drops the least important findings.
    """
    agreed_by = agreed_by or {}
    ranked = sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 9))

    line_comments: list[dict] = []
    file_comments: list[dict] = []
    unplaceable: list[Finding] = []
    held_back = 0

    for finding in ranked:
        if _SEVERITY_ORDER.get(finding.severity, 9) > _SEVERITY_ORDER[MIN_INLINE_SEVERITY]:
            held_back += 1
            continue
        anchor = finding.anchor
        if anchor is None or anchor.state is AnchorState.NONE:
            unplaceable.append(finding)
            continue
        if len(line_comments) + len(file_comments) >= MAX_INLINE_COMMENTS:
            held_back += 1
            continue
        body = comment_body(finding, agreed_by.get(finding.id))
        if anchor.state is AnchorState.FILE:
            # GitHub stores these at line 1, so without this it reads as a
            # confident claim about line 1. Say the line is unknown.
            body = (
                "<sub>📄 File-level: this finding's line could not be resolved, "
                "so it is attached to the file rather than a line.</sub>\n\n"
            ) + body
            file_comments.append(anchor.as_comment(body))
        else:
            line_comments.append(anchor.as_comment(body))

    return line_comments, file_comments, unplaceable, held_back


async def post_review(
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    body: str,
    line_comments: list[dict],
    file_comments: list[dict],
    head_sha: str,
) -> tuple[int, list[tuple[dict, str]]]:
    """Post the review: one request for the line comments, then the file ones.

    File-level comments cannot travel inside a review (see ``build_comments``),
    so they follow individually — one notification each, which is why anchoring
    to a *line* is worth the trouble.

    If the batch is rejected (usually because the head moved) the body posts
    alone and every comment is retried individually, so one bad line cannot
    discard a correct review.

    Returns ``(accepted, rejected)`` with GitHub's reason for each rejection.
    """
    accepted = 0
    rejected: list[tuple[dict, str]] = []
    batched = False

    try:
        await client.create_review(
            installation_id, owner, repo, pr_number,
            body=body, comments=line_comments, commit_id=head_sha,
        )
        accepted += len(line_comments)
        batched = True
    except client.GitHubError as exc:
        if "422" not in str(exc):
            raise
        log.warning(
            "batched review rejected for %s/%s#%s (%s) — retrying comment by comment",
            owner, repo, pr_number, str(exc)[:160],
        )
        # The body belongs on the PR even if every comment fails.
        await client.create_review(
            installation_id, owner, repo, pr_number, body=body, comments=[], commit_id=head_sha
        )

    one_at_a_time = file_comments if batched else line_comments + file_comments
    for comment in one_at_a_time:
        try:
            await client.create_review_comment(
                installation_id, owner, repo, pr_number, commit_id=head_sha, **comment
            )
            accepted += 1
        except client.GitHubError as exc:
            rejected.append((comment, str(exc)[:200]))
    return accepted, rejected
