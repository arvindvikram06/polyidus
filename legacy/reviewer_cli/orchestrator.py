from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from legacy.reviewer_cli.mcp.fs_client import FsBackendError, repo_tools
from legacy.reviewer_cli.mcp.github_client import GitHubMcpError, read_tools
from legacy.reviewer_cli.session import save_session
from legacy.reviewer_cli.sources.base import ReviewSource, ReviewTarget
from legacy.reviewer_cli.sources.staged import StagedGitSource
from reviewer.agents.llm import LLMError
from reviewer.config import MASTER_DISPATCH_CAP, MAX_DIFF_INPUT_TOKENS
from reviewer.core.master import MasterTraceEntry, run_master_loop
from reviewer.core.tracer import tracer

# `anchor_findings` now lives with the rest of the anchoring logic; re-exported
# here so existing callers keep working.
from reviewer.models.anchor import AnchorState, anchor_findings
from reviewer.models.diff_context import DiffContext, count_tokens, slice_diff
from reviewer.models.findings import Finding


class ReviewReport(BaseModel):
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    master_trace: list[MasterTraceEntry] = Field(default_factory=list)
    cap_reached: bool = False
    routing_summary: str = ""
    # Why the review stopped early, when it stopped early but still produced
    # findings. A partial review must never read as a clean one.
    aborted: str | None = None
    target: ReviewTarget | None = None
    # Stored alongside the session as diff.patch rather than inside it, and
    # excluded from the JSON body on save.
    diff_text: str = ""


def _build_summary(
    findings: list[Finding],
    trace: list[MasterTraceEntry],
    cap_reached: bool,
    aborted: str | None = None,
) -> str:
    if findings:
        files = len({f.file_path for f in findings})
        base = f"{len(findings)} finding(s) across {files} file(s)"
    else:
        base = "No findings"

    if trace:
        agents = len({entry.subagent for entry in trace})
        base += f" from {len(trace)} specialist run(s) across {agents} specialist(s)."
    else:
        base += "."

    failed = [entry for entry in trace if entry.error]
    if failed:
        base += f" {len(failed)} run(s) failed; the review is incomplete."
    if cap_reached:
        base += (
            f" The master dispatch cap ({MASTER_DISPATCH_CAP}) was reached; "
            "the review may be incomplete."
        )
    if aborted:
        base += (
            f" PARTIAL REVIEW — the master loop stopped early ({aborted}); "
            "the findings above are real, but some areas were never reviewed."
        )
    return base


def _repo_context(target: ReviewTarget) -> str:
    """Tell specialists which repository and commit their tools are reading.

    `get_file_contents` takes `path` as an *optional* argument: called without
    one it returns the repository root, so a model that does not know what it
    is looking at loops on directory listings until its budget is gone.
    """
    if target.kind != "pull_request":
        return ""
    return (
        f"REPOSITORY: {target.owner}/{target.repo}\n"
        f"You are reviewing pull request #{target.number} at commit "
        f"{target.head_sha or 'HEAD'} (base branch: {target.base_ref or 'unknown'}).\n"
        "Your tools already read this repository at that commit — do not pass "
        "owner, repo or ref, they are supplied for you. For `get_file_contents` "
        "you MUST pass `path` (a repository-relative file path, e.g. one of the "
        "files in scope below); omitting it just returns a directory listing. "
        f"For `search_code`, scope queries with `repo:{target.owner}/{target.repo}` "
        "— but note it relies on GitHub's code-search index, which lags for "
        "recently pushed code and may return nothing; prefer reading files "
        "directly by path. Repeating a call with identical arguments returns an "
        "identical result, so if a lookup fails, change the question or report "
        "what you already know."
    )


def _postable_counts(findings: list[Finding]) -> str:
    states = [f.anchor.state for f in findings if f.anchor]
    inline = sum(1 for s in states if s is AnchorState.LINE)
    file_level = sum(1 for s in states if s is AnchorState.FILE)
    orphan = sum(1 for s in states if s is AnchorState.NONE)
    return f"{inline} inline, {file_level} file-level, {orphan} unanchorable"


async def run_review(
    repo_root: Path, source: ReviewSource | None = None
) -> ReviewReport:
    source = source or StagedGitSource(repo_root)
    diff_context, target = await source.load()

    def _report(summary: str, **kwargs) -> ReviewReport:
        report = ReviewReport(summary=summary, target=target, **kwargs)
        save_session(report, repo_root, target)
        return report

    if not diff_context.diff_text.strip():
        return _report("No changes to review.")

    # WAF workaround: skip pom.xml to prevent 403 Forbidden errors
    filtered_files = [f for f in diff_context.changed_files if not f.endswith("pom.xml")]
    if not filtered_files:
        return _report("No valid changes to review (or all were excluded).")

    diff_context = DiffContext(
        diff_text=slice_diff(diff_context.diff_text, filtered_files),
        changed_files=filtered_files,
    )

    diff_tokens = count_tokens(diff_context.diff_text)
    if diff_tokens > MAX_DIFF_INPUT_TOKENS:
        return _report(
            f"Diff is too large to review: {diff_tokens} tokens exceeds the "
            f"{MAX_DIFF_INPUT_TOKENS} token limit. Review a smaller change."
        )

    # A PR review reads the repository from GitHub; a staged review reads the
    # local working tree through the sandboxed fs_server.
    try:
        tools = await (
            read_tools(target.owner, target.repo, target.head_sha)
            if target.kind == "pull_request"
            else repo_tools()
        )
        result = await run_master_loop(
            diff_context, tools, repo_context=_repo_context(target)
        )
    except (FsBackendError, GitHubMcpError) as exc:
        return _report(f"Review aborted: {exc}.", diff_text=diff_context.diff_text)
    except LLMError as exc:
        return _report(
            f"Review aborted: master agent failed ({exc}).",
            diff_text=diff_context.diff_text,
        )

    tracer.review_end(len(result.findings), len(result.trace))
    anchor_findings(result.findings, diff_context.diff_text)

    summary = _build_summary(
        result.findings, result.trace, result.cap_reached, result.aborted
    )
    if result.findings:
        summary += f" Anchoring: {_postable_counts(result.findings)}."

    return _report(
        summary,
        findings=result.findings,
        master_trace=result.trace,
        cap_reached=result.cap_reached,
        routing_summary=result.routing_summary,
        aborted=result.aborted,
        diff_text=diff_context.diff_text,
    )
