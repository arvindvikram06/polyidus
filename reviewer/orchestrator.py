from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from reviewer.config import MASTER_DISPATCH_CAP, MAX_DIFF_INPUT_TOKENS
from reviewer.diff_context import DiffContext, count_tokens
from reviewer.findings import Finding
from reviewer.git_utils import ensure_git_repo, get_staged_diff, get_staged_files
from reviewer.llm import LLMError
from reviewer.fs_client import FsBackendError, repo_tools
from reviewer.master import MasterTraceEntry, run_master_loop
from reviewer.session import save_session
from reviewer.tracer import tracer


class ReviewReport(BaseModel):
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    master_trace: list[MasterTraceEntry] = Field(default_factory=list)
    cap_reached: bool = False
    routing_summary: str = ""


def _build_summary(findings: list[Finding], trace: list[MasterTraceEntry], cap_reached: bool) -> str:
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
    return base


async def run_review(repo_root: Path) -> ReviewReport:
    ensure_git_repo(repo_root)

    staged_diff = get_staged_diff(repo_root)
    if not staged_diff.strip():
        report = ReviewReport(summary="No staged changes to review.")
        save_session(report, repo_root)
        return report

    staged_files = get_staged_files(repo_root)
    # WAF workaround: skip pom.xml to prevent 403 Forbidden errors
    filtered_files = [f for f in staged_files if not f.endswith("pom.xml")]
    
    if not filtered_files:
        report = ReviewReport(summary="No valid staged changes to review (or all were excluded).")
        save_session(report, repo_root)
        return report

    from reviewer.diff_context import slice_diff
    filtered_diff = slice_diff(staged_diff, filtered_files)

    diff_context = DiffContext(diff_text=filtered_diff, changed_files=filtered_files)

    diff_tokens = count_tokens(diff_context.diff_text)
    if diff_tokens > MAX_DIFF_INPUT_TOKENS:
        report = ReviewReport(
            summary=(
                f"Staged diff is too large to review: {diff_tokens} tokens exceeds the "
                f"{MAX_DIFF_INPUT_TOKENS} token limit. Stage a smaller change or split the commit."
            )
        )
        save_session(report, repo_root)
        return report

    try:
        tools = await repo_tools()
        result = await run_master_loop(diff_context, tools)
    except FsBackendError as exc:
        failure = str(exc)
    except LLMError as exc:
        failure = f"master agent failed ({exc})"
    else:
        failure = None

    if failure is not None:
        report = ReviewReport(summary=f"Review aborted: {failure}.")
        save_session(report, repo_root)
        return report

    tracer.review_end(len(result.findings), len(result.trace))

    report = ReviewReport(
        summary=_build_summary(result.findings, result.trace, result.cap_reached),
        findings=result.findings,
        master_trace=result.trace,
        cap_reached=result.cap_reached,
        routing_summary=result.routing_summary,
    )
    save_session(report, repo_root)
    return report
