"""Run a review for one job, and persist what it found.

This is the bot's orchestration. It deliberately does *not* call
``legacy.reviewer_cli.orchestrator.run_review`` — that function owns its own input
(a ``ReviewSource``), its own credential (the ``GITHUB_TOKEN`` PAT) and its own
persistence (JSON session files in the repo under review). All three differ
here: the diff arrives in the webhook's wake, the credential is a GitHub App
installation token, and the output goes to Postgres.

What *is* reused is the part worth reusing — the engine:

``run_master_loop``   the master plans, specialists run concurrently on
                      per-file diff slices, failures are isolated and partial
                      results salvaged
``anchor_findings``   every finding resolved to LINE / FILE / NONE by parsing
                      hunk headers, never by trusting a model's line number

Nothing here posts to GitHub. Step 6 adds that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bot.github import auth, client
from bot.review import store
from bot.review.adjudicate import adjudicate
from bot.review.local_tools import repo_tools
from bot.review.locate import resolve_line_ranges
from bot.workspace import WorkspaceError, checkout
from reviewer.config import MAX_DIFF_INPUT_TOKENS
from reviewer.core.master import run_master_loop
from reviewer.models.anchor import AnchorState, anchor_findings, parse_hunks
from reviewer.models.diff_context import DiffContext, count_tokens

log = logging.getLogger("bot.review")


@dataclass
class ReviewOutcome:
    """What happened, in a form the worker can report without re-querying."""

    head_sha: str
    changed_files: list[str]
    diff_tokens: int
    findings: list[Any] = field(default_factory=list)
    inserted: int = 0
    deduplicated: int = 0
    anchored: dict[str, int] = field(default_factory=dict)
    routing_summary: str = ""
    aborted: str | None = None
    skipped: str | None = None
    run_id: Any = None
    # Specialists that produced no usable output. Not findings — the reason the
    # review is incomplete, which the reader has to be told about explicitly.
    # Silence here would let a partial review read as a clean one.
    failed_specialists: list[str] = field(default_factory=list)
    # Written by the adjudication pass: the paragraph a reviewer reads first.
    summary_text: str = ""
    # finding id -> other specialists that independently found the same defect.
    # Independent agreement is signal, so it survives the merge.
    agreed_by: dict[str, list[str]] = field(default_factory=dict)
    merged: int = 0
    dropped: list[tuple[Any, str]] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.skipped is None


def _repo_context(owner: str, repo: str, pr_number: int, head_sha: str, base_ref: str) -> str:
    """Tell specialists what their tools are pointed at.

    Without this a specialist does not know which repository it is reading, and
    `get_file_contents` called without a path returns a *successful* directory
    listing — so a model that forgot the argument gets no signal and loops on
    listings until its budget is gone.
    """
    return (
        f"REPOSITORY: {owner}/{repo}\n"
        f"You are reviewing pull request #{pr_number} at commit {head_sha} "
        f"(base branch: {base_ref}).\n\n"
        "You have a full checkout of the repository at THIS commit, including "
        "everything this pull request adds. All three tools take "
        "repository-relative paths:\n"
        "  `list_directory(path)` — what is in a directory. Start at `.`\n"
        "  `search_code(pattern)` — regex across the repository, returns "
        "file:line\n"
        "  `read_file(path)`      — the contents of one file\n\n"
        "A typical lookup: search for the symbol, then read the file the match "
        "is in. If a search returns nothing the symbol genuinely is not there — "
        "this reads the actual files, not an index, so an empty result is an "
        "answer rather than a failure.\n\n"
        "Use them. A finding about a symbol the diff only *uses* — a type, a "
        "base class, a config value — is unverified until you have opened the "
        "file where that symbol is defined, and an unverified finding must be "
        "reported at `info` severity. You can open it, so open it.\n\n"
        "Repeating a call with identical arguments returns an identical result. "
        "If a lookup fails, change the question or report what you already know."
    )


def _changed_files(diff_text: str) -> list[str]:
    """Post-image paths, which is what `git diff --name-only` reports."""
    prefix = "diff --git a/"
    return [
        line[len(prefix) :].split(" b/")[0]
        for line in diff_text.splitlines()
        if line.startswith(prefix)
    ]


async def review_pull_request(
    *,
    job_id: int,
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
) -> ReviewOutcome:
    token = await auth.installation_token(installation_id)

    # Resolve the commit ONCE. Everything after this refers to `head_sha`
    # rather than to a branch, which is what makes a push during the review
    # detectable instead of silently invalidating every line number.
    pull = await client.get_pull(installation_id, owner, repo, pr_number)
    head_sha = pull["head"]["sha"]
    base_sha = (pull.get("base") or {}).get("sha")
    base_ref = (pull.get("base") or {}).get("ref") or "unknown"

    diff_text = await client.get_pull_diff(installation_id, owner, repo, pr_number)
    files = _changed_files(diff_text)

    if not diff_text.strip() or not files:
        return ReviewOutcome(head_sha, [], 0, skipped="the diff is empty")

    # The token gate. Measured with cl100k_base against a GLM model, so it is
    # an approximation with unknown error — hence a gate rather than a budget.
    diff_tokens = count_tokens(diff_text)
    if diff_tokens > MAX_DIFF_INPUT_TOKENS:
        return ReviewOutcome(
            head_sha,
            files,
            diff_tokens,
            skipped=(
                f"the diff is {diff_tokens:,} tokens, over the "
                f"{MAX_DIFF_INPUT_TOKENS:,} limit"
            ),
        )

    run_id = await store.open_run(
        job_id=job_id,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
    )

    # Specialists read from a checkout of this exact commit, not through the
    # API. Measured: reviewing through the API, three specialists opened one
    # file between them and every code search returned nothing, because
    # `search_code` has no `ref` and cannot see a pull request's own code.
    #
    # This is a clone to READ. Nothing below runs anything from the repository,
    # so none of the risk of executing an untrusted contributor's code applies.
    # The tools can only read files beneath the checkout root.
    diff_context = DiffContext(diff_text=diff_text, changed_files=files)
    try:
        async with checkout(owner, repo, head_sha, token) as repo_root:
            tools = repo_tools(repo_root)
            log.info(
                "run %s: %d files, %d tokens, %d tools, head %s, checkout %s",
                run_id, len(files), diff_tokens, len(tools), head_sha[:8], repo_root,
            )
            result = await run_master_loop(
                diff_context,
                tools,
                repo_context=_repo_context(owner, repo, pr_number, head_sha, base_ref),
            )

            # Resolve each finding's quoted source line to a real line number,
            # while the checkout still exists. Counting lines through diff
            # hunks is arithmetic the model gets wrong; finding a quoted string
            # in a file is a search we get right.
            located = resolve_line_ranges(
                result.findings, repo_root, parse_hunks(diff_text)
            )
            log.info("run %s: line numbers %s", run_id, located)
    except WorkspaceError as exc:
        await store.close_run(run_id, summary=f"aborted: {exc}", aborted=str(exc))
        raise

    # A specialist that died produces a Finding marked `is_failure` rather than
    # nothing, so the CLI can show it in a terminal report. The bot must not
    # publish those: `file_path` on such a record is arbitrary, so posting it
    # means commenting on a file that has nothing wrong with it. They belong in
    # the summary as "a specialist failed", which is what the reader needs to
    # know — that the review is incomplete.
    failures = [f for f in result.findings if f.is_failure]
    findings = [f for f in result.findings if not f.is_failure]
    for failure in failures:
        # Logged in full, not just counted. A failed specialist is the most
        # expensive event in a review — a whole area goes unreviewed — and the
        # only way to fix it is to see what the model actually said.
        log.warning("run %s: %s produced no usable output.\n%s",
                    run_id, failure.subagent, failure.message)
    result.findings = findings

    # Specialists run concurrently on overlapping files and cannot see each
    # other, so the same defect arrives several times at several severities.
    # One pass groups them, scores each group once against the rubric, and
    # writes the summary. It never rewrites a finding's words.
    before_adjudication = len(result.findings)
    verdict = await adjudicate(result.findings)
    result.findings = verdict.findings
    merged_count = before_adjudication - len(verdict.findings) - len(verdict.dropped)

    # Placement is decided here, by parsing the diff — never by asking the
    # model where its comment should go. A miscounted line would otherwise post
    # a confident comment on unrelated code, and GitHub only rejects lines
    # outside the diff entirely.
    anchor_findings(result.findings, diff_text)
    states = [f.anchor.state for f in result.findings if f.anchor]
    anchored = {
        "line": sum(1 for s in states if s is AnchorState.LINE),
        "file": sum(1 for s in states if s is AnchorState.FILE),
        "none": sum(1 for s in states if s is AnchorState.NONE),
    }

    inserted, deduplicated = await store.save_findings(
        run_id,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        findings=result.findings,
    )
    # Failures are stored too, marked `suppressed` so nothing publishes them.
    # Filtering them out of the ledger entirely was a mistake: it removed the
    # only record of *why* a specialist produced nothing, which is exactly the
    # question worth answering afterwards.
    if failures:
        await store.save_findings(
            run_id,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            findings=failures,
            status="suppressed",
        )
    await store.save_scratchpads(run_id, result.trace)

    failed_specialists = sorted({f.subagent for f in failures})
    summary = _summary(result, anchored, inserted, deduplicated, failed_specialists)
    await store.close_run(
        run_id,
        summary=summary,
        routing=[e.model_dump(mode="json") for e in result.trace],
        aborted=result.aborted,
    )

    return ReviewOutcome(
        head_sha=head_sha,
        changed_files=files,
        diff_tokens=diff_tokens,
        findings=result.findings,
        inserted=inserted,
        deduplicated=deduplicated,
        anchored=anchored,
        routing_summary=result.routing_summary,
        aborted=result.aborted,
        run_id=run_id,
        failed_specialists=failed_specialists,
        summary_text=verdict.summary,
        agreed_by=verdict.agreed_by,
        merged=merged_count,
        dropped=verdict.dropped,
    )


def _summary(
    result: Any,
    anchored: dict[str, int],
    inserted: int,
    deduplicated: int,
    failed_specialists: list[str],
) -> str:
    if result.findings:
        files = len({f.file_path for f in result.findings})
        text = f"{len(result.findings)} finding(s) across {files} file(s)"
    else:
        text = "no findings"

    if result.trace:
        agents = len({e.subagent for e in result.trace})
        text += f" from {len(result.trace)} specialist run(s) across {agents} specialist(s)"
    text += "."

    failed = [e for e in result.trace if e.error]
    if failed:
        text += f" {len(failed)} run(s) failed; the review is incomplete."
    if failed_specialists:
        # Named, not counted: "architecture produced nothing" tells the reader
        # which areas of the change were never actually looked at.
        text += (
            f" No usable output from: {', '.join(failed_specialists)} — "
            "those areas were not reviewed."
        )
    if deduplicated:
        text += f" {deduplicated} duplicate(s) collapsed."
    if result.cap_reached:
        text += " The master dispatch cap was reached; the review may be incomplete."
    if result.aborted:
        # A partial review must never read as a clean one.
        text += (
            f" PARTIAL REVIEW — the master loop stopped early ({result.aborted}); "
            "the findings are real, but some areas were never reviewed."
        )
    if result.findings:
        text += (
            f" Anchoring: {anchored['line']} inline, {anchored['file']} file-level, "
            f"{anchored['none']} unanchorable."
        )
    return text
