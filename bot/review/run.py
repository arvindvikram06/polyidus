"""Run a review for one job and return what it found.

Built on two pieces of the engine:

``run_master_loop``   the master plans, specialists run concurrently on
                      per-file diff slices, failures isolated, partials salvaged
``anchor_findings``   every finding resolved to LINE / FILE / NONE by parsing
                      hunk headers, never by trusting a model's line number

Nothing is written to a database. Findings go to the worker, which posts each
inline; the thread that creates is the record — finding, reply, and decision.
Postgres holds only the job queue.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from bot import config as bot_config
from bot.github import auth, client
from bot.review import history as history_mod
from bot.review.adjudicate import adjudicate
from bot.review.local_tools import repo_tools
from bot.review.locate import resolve_line_ranges
from bot.workspace import WorkspaceError, checkout
from reviewer.config import MAX_DIFF_INPUT_TOKENS, TRUST_MODEL_LINE_NUMBERS
from reviewer.core.master import run_master_loop
from reviewer.models.anchor import AnchorState, anchor_findings, parse_hunks
from reviewer.models.diff_context import DiffContext, count_tokens

log = logging.getLogger("bot.review")


@dataclass
class ReviewOutcome:
    """What happened, in a form the worker can report without re-querying."""

    head_sha: str
    changed_files: list[str]
    findings: list[Any] = field(default_factory=list)
    anchored: dict[str, int] = field(default_factory=dict)
    aborted: str | None = None
    skipped: str | None = None
    # Not findings — the reason the review is incomplete. Silence here would
    # let a partial review read as a clean one.
    failed_specialists: list[str] = field(default_factory=list)
    # Written by the adjudication pass: the paragraph a reviewer reads first.
    summary_text: str = ""
    # finding id -> other specialists that independently found the same defect.
    # Independent agreement is signal, so it survives the merge.
    agreed_by: dict[str, list[str]] = field(default_factory=dict)
    dropped: list[tuple[Any, str]] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.skipped is None


def _repo_context(owner: str, repo: str, pr_number: int, head_sha: str, base_ref: str) -> str:
    """Tell specialists what their tools are pointed at.

    Without it a specialist does not know which repository it is reading, and a
    file read with no path used to return a *successful* directory listing — so
    a model that forgot the argument looped until its budget was gone.
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
        "  `read_file(path)`      — one file, with its real line numbers in "
        "the margin\n\n"
        "Those line numbers are the answer to 'where is this?'. Read the number "
        "off the margin and report it — never count lines yourself, and never "
        "work one out from a diff hunk header.\n\n"
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
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
) -> ReviewOutcome:
    token = await auth.installation_token(installation_id)

    # Resolve the commit ONCE. Referring to `head_sha` rather than a branch is
    # what makes a push mid-review detectable rather than silently wrong.
    pull = await client.get_pull(installation_id, owner, repo, pr_number)
    head_sha = pull["head"]["sha"]
    base_ref = (pull.get("base") or {}).get("ref") or "unknown"

    diff_text = await client.get_pull_diff(installation_id, owner, repo, pr_number)
    files = _changed_files(diff_text)

    if not diff_text.strip() or not files:
        return ReviewOutcome(head_sha, [], skipped="the diff is empty")

    # Measured with cl100k_base against a different tokenizer than the model's,
    # so it is an approximation — hence a gate rather than a budget.
    diff_tokens = count_tokens(diff_text)
    if diff_tokens > MAX_DIFF_INPUT_TOKENS:
        return ReviewOutcome(
            head_sha,
            files,
            skipped=(
                f"the diff is {diff_tokens:,} tokens, over the "
                f"{MAX_DIFF_INPUT_TOKENS:,} limit"
            ),
        )

    # For the log only. Findings live as inline threads on GitHub, replies and
    # resolved flag included — the part a table of ours could never carry.
    run_id = uuid.uuid4()

    # What this PR already knows: our previous comments, any reply, and which
    # threads a human resolved. Read before planning, so the master spends the
    # budget on what changed.
    prior_text, prior_roots = await history_mod.fetch(
        installation_id, owner, repo, pr_number, bot_config.BOT_LOGIN
    )
    if prior_text:
        log.info("run %s: %d previous thread(s) in context", run_id, len(prior_roots))

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
                history=prior_text,
                repo_context=_repo_context(owner, repo, pr_number, head_sha, base_ref),
            )

            # Resolve each quoted line to a real line number while the checkout
            # exists. Counting through hunks is arithmetic models get wrong;
            # searching for a quoted string is a search we get right.
            if TRUST_MODEL_LINE_NUMBERS:
                # The comparison arm: post the number the model counted, with
                # nothing verifying it. A wrong line here is invisible.
                log.warning(
                    "run %s: TRUST_MODEL_LINE_NUMBERS is on — posting the "
                    "counted line without checking it against the file",
                    run_id,
                )
            else:
                located = resolve_line_ranges(
                    result.findings, repo_root, parse_hunks(diff_text)
                )
                log.info("run %s: line numbers %s", run_id, located)
    except WorkspaceError as exc:
        log.error("run %s aborted: %s", run_id, exc)
        raise

    # A dead specialist produces a Finding marked `is_failure`. It must not be
    # published: `file_path` on such a record is arbitrary, so posting it
    # comments on a file with nothing wrong. It belongs in the summary.
    failures = [f for f in result.findings if f.is_failure]
    findings = [f for f in result.findings if not f.is_failure]
    for failure in failures:
        # In full, not counted: a failed specialist leaves a whole area
        # unreviewed, and only the model's own words explain why.
        log.warning("run %s: %s produced no usable output.\n%s",
                    run_id, failure.subagent, failure.message)
    result.findings = findings

    # Concurrent specialists on overlapping files cannot see each other, so the
    # same defect arrives several times at several severities. One pass groups
    # and re-scores them; it never rewrites a finding's words.
    verdict = await adjudicate(result.findings)
    result.findings = verdict.findings

    # Placement comes from parsing the diff, never from asking the model. A
    # miscounted line would post a confident comment on unrelated code, and
    # GitHub only rejects lines outside the diff entirely.
    anchor_findings(result.findings, diff_text)
    states = [f.anchor.state for f in result.findings if f.anchor]
    anchored = {
        "line": sum(1 for s in states if s is AnchorState.LINE),
        "file": sum(1 for s in states if s is AnchorState.FILE),
        "none": sum(1 for s in states if s is AnchorState.NONE),
    }

    failed_specialists = sorted({f.subagent for f in failures})
    summary = _summary(result, anchored, failed_specialists)
    log.info("run %s: %s", run_id, summary)

    return ReviewOutcome(
        head_sha=head_sha,
        changed_files=files,
        findings=result.findings,
        anchored=anchored,
        aborted=result.aborted,
        failed_specialists=failed_specialists,
        summary_text=verdict.summary,
        agreed_by=verdict.agreed_by,
        dropped=verdict.dropped,
    )


def _summary(
    result: Any,
    anchored: dict[str, int],
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
        # Named, not counted — which areas were never looked at.
        text += (
            f" No usable output from: {', '.join(failed_specialists)} — "
            "those areas were not reviewed."
        )
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
