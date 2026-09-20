"""Turn a finished review into one pending review on the pull request.

Nothing here is driven by a model. The findings are already written, already
anchored, and already filtered by a human (or by an explicit severity floor);
this module only formats them and makes a single POST.

The review is created PENDING — no ``event`` field — which means it is a draft
visible only to the token's owner. They open the PR, delete anything wrong, and
click Submit. Until they do, the PR author sees nothing, so a bad finding costs
a click rather than a retraction.
"""

from __future__ import annotations

from legacy.reviewer_cli.mcp.github_client import (
    GitHubMcpError,
    call_tool_checked,
    review_tools,
)
from reviewer.models.anchor import AnchorState
from reviewer.models.findings import Finding, FindingStatus, Severity

_SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def comment_body(finding: Finding) -> str:
    lines = [f"**{finding.severity.value.upper()}: {finding.title}**", "", finding.message]
    if finding.suggested_patch:
        lines += ["", "```suggestion", finding.suggested_patch.rstrip(), "```"]
    lines += ["", f"<sub>`{finding.subagent}` specialist</sub>"]
    return "\n".join(lines)


def _review_body(
    report_summary: str, orphans: list[Finding], skipped: int
) -> str:
    lines = ["## Automated review", "", report_summary]
    if orphans:
        lines += [
            "",
            "### Findings without a line in this diff",
            "",
            ("These concern files the pull request does not touch, so they could "
             "not be attached inline:"),
            "",
        ]
        for finding in orphans:
            lines.append(
                f"- **{finding.severity.value.upper()}** `{finding.file_path}` — "
                f"{finding.title}: {finding.message}"
            )
    if skipped:
        lines += ["", f"<sub>{skipped} finding(s) below the severity floor were not posted.</sub>"]
    return "\n".join(lines)


def build_review(
    findings: list[Finding],
    summary: str,
    min_severity: Severity = Severity.LOW,
) -> tuple[str, list[dict], list[Finding]]:
    """Split findings into inline comments, body text, and what was dropped.

    Returns ``(body, comments, posted)``. ``posted`` is the findings that became
    a comment, so the caller can mark exactly those and no others.
    """
    floor = _SEVERITY_ORDER[min_severity]
    eligible, skipped = [], 0
    for finding in findings:
        if finding.status is FindingStatus.REJECTED:
            continue
        if _SEVERITY_ORDER[finding.severity] < floor:
            skipped += 1
            continue
        eligible.append(finding)

    comments: list[dict] = []
    posted: list[Finding] = []
    orphans: list[Finding] = []

    for finding in eligible:
        anchor = finding.anchor
        if anchor is None or anchor.state is AnchorState.NONE:
            orphans.append(finding)
            continue
        comments.append(anchor.as_comment(comment_body(finding)))
        posted.append(finding)

    return _review_body(summary, orphans, skipped), comments, posted


class StaleReviewError(Exception):
    """The pull request moved after the review ran, so line numbers are wrong."""


async def _current_head_sha(tools, owner: str, repo: str, number: int) -> str:
    """Read the PR's current head through the writable toolset.

    Done here rather than reusing the source's read connection so posting is
    self-contained: `reviewer post` is a separate command from `reviewer review`
    and may run much later, against a PR that has moved in between.
    """
    from legacy.reviewer_cli.sources.github_pr import _dig

    meta = await call_tool_checked(
        tools, "pull_request_read", method="get",
        owner=owner, repo=repo, pullNumber=number,
    )
    return _dig(meta, "head", "sha") or ""


async def post_pending_review(
    report,
    min_severity: Severity = Severity.LOW,
    tools: dict | None = None,
    allow_stale: bool = False,
) -> dict:
    """Leave a pending review on the PR for a human to edit and submit.

    Three MCP calls plus one per comment: start a pending review, add each
    comment to it, then stop. `submit_review` is deliberately never called —
    submitting is the human's act, performed in GitHub's UI, and is the only
    thing that makes any of this visible to the pull request author.
    """
    target = report.target
    if target is None or not target.is_postable:
        raise GitHubMcpError("this review has no pull request to post to")

    owner, repo, number = target.owner, target.repo, target.number
    tools = tools if tools is not None else await review_tools(owner, repo)

    # Someone pushing between review and post moves every line the findings
    # point at. Posting anyway would attach confident comments to unrelated
    # code — the failure that makes a reviewer look broken.
    current_sha = await _current_head_sha(tools, owner, repo, number)
    if current_sha and target.head_sha and current_sha != target.head_sha and not allow_stale:
        raise StaleReviewError(
            f"PR #{number} has moved since this review ran "
            f"({target.head_sha[:8]} -> {current_sha[:8]}). "
            "Re-run the review; the stored line numbers no longer match."
        )

    body, comments, posted = build_review(report.findings, report.summary, min_severity)

    # No `event` — GitHub's own docs for this tool: "If 'event' is omitted, a
    # pending review is created." That omission is the entire human gate.
    await call_tool_checked(
        tools, "pull_request_review_write", method="create",
        owner=owner, repo=repo, pullNumber=number, body=body,
        commitID=target.head_sha or None,
    )

    added: list[Finding] = []
    rejected: list[tuple[Finding, str]] = []
    for finding, comment in zip(posted, comments, strict=True):
        try:
            await call_tool_checked(
                tools, "add_comment_to_pending_review",
                owner=owner, repo=repo, pullNumber=number,
                path=comment["path"], body=comment["body"],
                line=comment.get("line"), side=comment.get("side"),
                startLine=comment.get("start_line"), startSide=comment.get("start_side"),
                # Required, and the enum is upper case: ['FILE', 'LINE'].
                subjectType="FILE" if comment.get("subject_type") else "LINE",
            )
        except Exception as exc:
            # One rejected comment must not discard the rest of the review; the
            # pending review already exists and the others still belong on it.
            finding.status = FindingStatus.HELD
            rejected.append((finding, str(exc)))
            continue
        finding.status = FindingStatus.POSTED
        added.append(finding)

    return {
        "pending": True,
        "url": target.url,
        "comments_added": len(added),
        "comments_rejected": len(rejected),
        "rejections": [(f.title, err[:200]) for f, err in rejected],
    }
