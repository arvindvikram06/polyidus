"""The worker: claims jobs and does the slow work GitHub would not wait for.

Step 3 scope deliberately stops short of reviewing. This process proves the
plumbing end to end — claim, lock, acknowledge, report, release — with no agent
and no model involved. Every later step replaces the body of ``handle_review``
and leaves this loop alone.

Getting this boring first matters because almost every plumbing failure
(signature, replay, loop guard, the ten-second response) produces symptoms
that look like a broken agent.

    python -m bot.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import asyncpg

from bot import config, db, locks
from bot.github import client
from bot.review.run import review_pull_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot.worker")

_stop = asyncio.Event()


# ------------------------------------------------------------- handlers -----
async def handle_review(job: asyncpg.Record) -> None:
    owner, repo, pr = job["owner"], job["repo"], job["pr_number"]
    inst = job["installation_id"]

    # Acknowledge first, and fast. From the human's side, nothing has happened
    # since they pressed enter; without a signal they assume it is broken and
    # comment again, which queues a second job.
    if job["trigger_comment_id"]:
        with contextlib.suppress(client.GitHubError):
            await client.react_to_issue_comment(inst, owner, repo, job["trigger_comment_id"])

    outcome = await review_pull_request(
        job_id=job["id"],
        installation_id=inst,
        owner=owner,
        repo=repo,
        pr_number=pr,
    )

    if not outcome.ran:
        log.info("review %s/%s#%s skipped: %s", owner, repo, pr, outcome.skipped)
        await client.post_issue_comment(
            inst, owner, repo, pr,
            f"**Nothing to review** — {outcome.skipped}.\n\n"
            f"<sub>requested by @{job['requested_by']}</sub>",
        )
        return

    log.info(
        "review %s/%s#%s done: %d finding(s), %d new, %d duplicate, anchoring %s",
        owner, repo, pr, len(outcome.findings),
        outcome.inserted, outcome.deduplicated, outcome.anchored,
    )

    # --- step 6 replaces this with a real review + inline comments -------
    # Findings exist and are anchored, but posting them as a pull request
    # review needs the fingerprint markers and the publisher. For now they are
    # reported in one comment so the run can be judged.
    await client.post_issue_comment(
        inst, owner, repo, pr, _review_comment(outcome, job["requested_by"])
    )


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _review_comment(outcome, requested_by: str) -> str:
    """The review, as one comment on the pull request.

    Deliberately one comment rather than inline review comments: a single
    place to read, nothing to submit, and no risk of a wrong line putting a
    confident remark on unrelated code. Each finding carries `file:line` in
    its heading instead, resolved from the quoted source line.
    """
    lines = [
        (
            f"**Review complete** at `{outcome.head_sha[:8]}` — "
            f"{len(outcome.findings)} finding(s) across "
            f"{len(outcome.changed_files)} file(s), "
            f"{outcome.diff_tokens:,} diff tokens."
        ),
        "",
    ]
    if outcome.aborted:
        lines += [f"> ⚠️ **Partial review.** {outcome.aborted}", ""]
    if outcome.failed_specialists:
        # Stated before the findings, not buried under them. A reader who sees
        # eight findings and no warning reasonably concludes the change was
        # fully reviewed; naming what failed is what stops that.
        lines += [
            "> ⚠️ **Incomplete.** No usable output from "
            + ", ".join(f"`{s}`" for s in outcome.failed_specialists)
            + " — whatever those specialists cover was not reviewed.",
            "",
        ]

    if outcome.summary_text:
        # The adjudicator's paragraph, before the list. A reviewer should be
        # able to stop reading after this and still know what matters.
        lines += [outcome.summary_text, "", "---", ""]

    if not outcome.findings:
        lines.append("_No findings._")
    else:
        for f in sorted(
            outcome.findings, key=lambda f: _SEVERITY_ORDER.get(f.severity.value, 9)
        ):
            where = f.file_path
            if f.line_range:
                where += f":{f.line_range[0]}"
            # Independent agreement is evidence, so it is shown rather than
            # thrown away with the duplicate — three specialists finding the
            # same thing is worth more than one finding it.
            also = outcome.agreed_by.get(f.id) or []
            credit = f.subagent
            if also:
                credit += ", " + ", ".join(also)
            lines += [
                f"#### `{where}`",
                f"**{f.severity.value.upper()}** — {f.title}",
                "",
                f"{f.message}",
                "",
                f"<sub>{credit}</sub>",
                "",
            ]
            if f.suggested_patch:
                lines += ["```suggestion", f.suggested_patch.rstrip(), "```", ""]

    if outcome.dropped:
        # Shown, not hidden. A finding the adjudicator threw away is a decision
        # someone may disagree with, and it should be auditable without psql.
        lines += [
            f"<details><summary>Dropped during review ({len(outcome.dropped)})</summary>",
            "",
        ]
        for finding, reason in outcome.dropped:
            lines.append(f"- **{finding.title}** (_{finding.subagent}_) — {reason}")
        lines += ["", "</details>", ""]

    lines += [
        "---",
        (
            f"<sub>{outcome.anchored.get('line', 0)} located to a line, "
            f"{outcome.anchored.get('file', 0)} file-level · "
            f"{outcome.merged} merged, {len(outcome.dropped)} dropped · "
            f"{outcome.inserted} new, {outcome.deduplicated} duplicate · "
            f"run `{outcome.run_id}` · requested by @{requested_by}</sub>"
        ),
    ]
    return "\n".join(lines)


async def handle_dispute(job: asyncpg.Record) -> None:
    """Placeholder until step 9.

    Enqueued replies are acknowledged and dropped. They cannot be acted on yet
    because there is no finding ledger to look the disputed finding up in, and
    no fingerprint marker in any comment body to find it by.
    """
    log.info(
        "dispute on %s/%s#%s by @%s — acknowledged, not handled until step 9",
        job["owner"], job["repo"], job["pr_number"], job["requested_by"],
    )
    if job["trigger_comment_id"]:
        with contextlib.suppress(client.GitHubError):
            await client.react_to_review_comment(
                job["installation_id"], job["owner"], job["repo"], job["trigger_comment_id"]
            )


HANDLERS = {"review": handle_review, "dispute": handle_dispute}


# ----------------------------------------------------------------- loop -----
async def run_job(job: asyncpg.Record) -> None:
    intent = job["intent"]
    label = f"job {job['id']} ({intent} {job['owner']}/{job['repo']}#{job['pr_number']})"
    try:
        async with locks.pr_lock(job["owner"], job["repo"], job["pr_number"]):
            await HANDLERS[intent](job)
        await db.finish_job(job["id"])
        log.info("%s done", label)
    except locks.LockBusy as exc:
        # Not a failure. Another worker has this PR; put it back and let it be
        # picked up once that one finishes.
        log.info("%s deferred: %s", label, exc)
        await db.finish_job(job["id"], error=f"deferred: {exc}")
    except Exception as exc:  # one bad job must not kill the loop
        log.exception("%s failed", label)
        await db.finish_job(job["id"], error=f"{type(exc).__name__}: {exc}")


async def main() -> None:
    problems = config.check()
    if problems:
        log.error("configuration incomplete: %s", "; ".join(problems))
        log.error("run: .venv/bin/python scripts/preflight.py")
        return

    log.info(
        "worker %s up — postgres=%s redis=%s",
        config.WORKER_ID,
        config.DATABASE_URL.split("@")[-1],
        config.REDIS_URL,
    )
    await db.pool()

    idle_logged = False
    while not _stop.is_set():
        try:
            job = await db.claim_job(config.WORKER_ID)
        except Exception:  # Postgres restarting, say
            log.exception("could not claim a job; retrying")
            await asyncio.sleep(5)
            continue

        if job is None:
            if not idle_logged:
                log.info("waiting for jobs")
                idle_logged = True
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_stop.wait(), timeout=config.POLL_INTERVAL_SECONDS)
            continue

        idle_logged = False
        await run_job(job)

    log.info("worker stopping")
    await db.close()
    await locks.close()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop.set)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    with contextlib.suppress(KeyboardInterrupt):
        loop.run_until_complete(main())
