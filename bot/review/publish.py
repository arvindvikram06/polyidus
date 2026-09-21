"""Turn a finished review into inline comments on the pull request.

Nothing here is driven by a model. The findings are already written, already
anchored to a line, and already judged; this module only formats them and
posts.

The shape follows one rule, which is the reason this module replaced a single
summary comment:

    Every finding goes inline, on the line it is about. The summary comment
    contains no findings at all.

An inline comment creates a resolvable thread. That thread *is* the record: it
carries the finding, the human's reply, and — once someone resolves it — the
decision. A findings list in one comment carries none of that. It cannot be
replied to per item, cannot be resolved per item, and needs a parallel database
to track what has already been said.

So the platform holds the review state, and the only thing left in Postgres is
the job queue — which is there because we own the webhook, not because a
review needs it.
"""

from __future__ import annotations

import logging
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

# Oswald caps inline comments per review at ten and drops the lowest-impact
# findings past that. The reasoning is sound and worth copying: a review with
# thirty inline comments is not read, it is dismissed. The cap is on what is
# *posted*, not on what is found — the rest are still recorded.
MAX_INLINE_COMMENTS = 10

# Anything below this is not posted as a comment. `info` is the severity a
# specialist must use when it could not verify a claim, and it is also where
# observations land — one real review posted "this is an observation, not a
# defect requiring immediate action" as an inline comment, nine lines above an
# unfixed SQL injection. A review is read in severity order or not at all, so
# an item that announces it needs no action is competing with the ones that do.
MIN_INLINE_SEVERITY = Severity.LOW


def comment_body(finding: Finding, also_found_by: list[str] | None = None) -> str:
    """One inline comment: the issue, its impact, and where the fix goes.

    Kept short on purpose. A comment that needs scrolling gets collapsed by
    GitHub, and a collapsed comment is an unread one.
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
    return "\n".join(lines)


def summary_body(
    *,
    head_sha: str,
    changed_files: int,
    posted: int,
    requested_by: str,
    unplaceable: list[Finding],
    held_back: int,
    overview: str = "",
    aborted: str | None = None,
    failed_specialists: list[str] | None = None,
) -> str:
    """The conversation-box comment. Context only — no findings.

    The one deliberate exception is ``unplaceable``: findings about files this
    pull request does not touch. They have no line to attach to, and dropping a
    real finding to satisfy a formatting rule is the wrong trade. They go in a
    collapsed block, clearly separated from the review itself.
    """
    lines = [
        (
            f"**Review complete** at `{head_sha[:8]}` — {changed_files} file(s) changed, "
            f"{posted} inline comment(s) posted."
        ),
        "",
    ]

    if aborted:
        lines += [f"> ⚠️ **Partial review.** {aborted}", ""]

    if failed_specialists:
        # Stated before anything else. A reader who sees a clean review and no
        # warning reasonably concludes the change was fully examined; naming
        # what failed is what stops that.
        lines += [
            "> ⚠️ **Incomplete.** No usable output from "
            + ", ".join(f"`{s}`" for s in failed_specialists)
            + " — whatever those specialists cover was not reviewed.",
            "",
        ]

    if overview:
        # An overview of the change is context, not a finding — it is what the
        # summary is *for*. The rule it must not break is listing the issues.
        lines += [overview, ""]

    lines += [
        (
            "Findings are inline on the lines they concern. Reply in a thread to "
            "disagree; resolve it when it is handled."
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

    The line/file split is not a style choice. A batched review's comments are
    ``DraftPullRequestReviewComment`` objects, which have no ``subject_type``
    field — so a single file-level comment in the array makes GitHub reject the
    **entire** review with a 422. Measured: a review of seven file-level
    findings failed completely until they were separated out.

    Sorted by severity so that if the cap bites, it drops the least important
    findings rather than whichever happened to come last.
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
            # GitHub stores a file-level comment as line 1 and renders it at the
            # top of the file, so without this it reads as a confident claim
            # about line 1. Say plainly that the line is unknown.
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
    so they follow as individual calls. That costs one notification each, which
    is why anchoring a finding to a *line* is worth the trouble.

    If the batch is still rejected — a line GitHub will not accept, usually
    because the head moved — the body is posted alone and every comment is
    retried individually, so one bad line cannot discard a correct review.

    Returns ``(accepted, rejected)``, each rejection being the comment and
    GitHub's reason, so a caller can log precisely what was lost.
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
        # The body still belongs on the pull request even if every comment fails.
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
