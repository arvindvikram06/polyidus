"""The worker: claims jobs and does the slow work GitHub would not wait for.

A review takes minutes; a webhook gets ~10s. The API writes a job row and
returns, this process picks it up. That split is the entire reason Postgres is
here — it holds no review state, only work that must survive a crash.

The loop is deliberately dull (claim, lock, handle, release): plumbing failures
mimic a broken agent, so the plumbing stays boring enough to rule out.

    python -m bot.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

import asyncpg

from bot import config, db, locks
from bot.github import auth, client
from bot.review import publish
from bot.review.local_tools import repo_tools
from bot.review.recheck import recheck
from bot.review.run import review_pull_request
from bot.workspace import checkout
from reviewer import config as reviewer_config

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

    # Acknowledge fast: without a signal the human assumes it is broken and
    # comments again, queueing a second job.
    if job["trigger_comment_id"] and not config.DRY_RUN:
        with contextlib.suppress(client.GitHubError):
            await client.react_to_issue_comment(inst, owner, repo, job["trigger_comment_id"])

    # Nothing to do if the head has not moved: same commit, same findings, and
    # a second copy of every comment. Reviews carry the commit they were posted
    # against, and a clean review still leaves one — hence reviews, not comments.
    pull = await client.get_pull(inst, owner, repo, pr)
    head_sha = pull["head"]["sha"]
    if not job["payload"].get("_force") and not config.DRY_RUN:
        reviewed = {
            r.get("commit_id")
            for r in await client.list_reviews(inst, owner, repo, pr)
            if (r.get("user") or {}).get("login") == config.BOT_LOGIN
        }
        if head_sha in reviewed:
            log.info("review %s/%s#%s: %s already reviewed, skipping",
                     owner, repo, pr, head_sha[:8])
            await client.post_issue_comment(
                inst, owner, repo, pr,
                f"**Already reviewed `{head_sha[:8]}`** — no new commits since the "
                "last review, so nothing has changed to look at.\n\n"
                "Push a change and tag me again, or say `@polyidus-bot review force` "
                "to re-run against the same commit.\n\n"
                f"<sub>requested by @{job['requested_by']}</sub>",
            )
            return

    outcome = await review_pull_request(
        installation_id=inst,
        owner=owner,
        repo=repo,
        pr_number=pr,
    )

    if not outcome.ran:
        log.info("review %s/%s#%s skipped: %s", owner, repo, pr, outcome.skipped)
        if config.DRY_RUN:
            return
        await client.post_issue_comment(
            inst, owner, repo, pr,
            f"**Nothing to review** — {outcome.skipped}.\n\n"
            f"<sub>requested by @{job['requested_by']}</sub>",
        )
        return

    log.info(
        "review %s/%s#%s done: %d finding(s), anchoring %s",
        owner, repo, pr, len(outcome.findings), outcome.anchored,
    )

    # Read from GitHub, not a table of ours: the comment a human can see is the
    # record, and a second copy could disagree with it.
    existing = await client.list_review_comments(inst, owner, repo, pr)
    roots = [c for c in existing if not c.get("in_reply_to_id")
             and (c.get("user") or {}).get("login") == config.BOT_LOGIN]

    findings, already = publish.split_already_said(
        outcome.findings, [c.get("body") or "" for c in roots]
    )
    if already:
        log.info(
            "%s/%s#%s: %d finding(s) already have a thread, not repeating",
            owner, repo, pr, len(already),
        )

    # Nothing here claims a finding is fixed. The summary used to count fixes
    # whenever a marker went missing from a run's output, which put "looks
    # fixed" on a live SQL injection the specialist had merely reworded.
    # Absence is not evidence — so confirming a fix is a specialist's job now:
    # bot/review/history.py tells the master to open the file and check, and
    # what it finds is reported as an ordinary finding in its own words.

    # Every finding inline, none in the summary: an inline comment is a
    # resolvable thread, and the thread is the record.
    line_comments, file_comments, unplaceable, held_back = publish.build_comments(
        findings, outcome.agreed_by
    )
    body = publish.summary_body(
        head_sha=outcome.head_sha,
        changed_files=len(outcome.changed_files),
        posted=len(line_comments) + len(file_comments),
        requested_by=job["requested_by"],
        unplaceable=unplaceable,
        held_back=held_back,
        already_said=len(already),
        overview=outcome.summary_text,
        aborted=outcome.aborted,
        failed_specialists=outcome.failed_specialists,
    )
    if config.DRY_RUN:
        _print_dry_run(owner, repo, pr, body, line_comments, file_comments, unplaceable)
        return

    accepted, rejected = await publish.post_review(
        inst, owner, repo, pr,
        body=body,
        line_comments=line_comments,
        file_comments=file_comments,
        head_sha=outcome.head_sha,
    )
    for comment, reason in rejected:
        log.warning("comment on %s rejected: %s", comment.get("path"), reason)
    log.info(
        "posted %s/%s#%s: %d on a line, %d file-level, %d unplaceable, "
        "%d held back, %d rejected, %d already said",
        owner, repo, pr, len(line_comments), len(file_comments),
        len(unplaceable), held_back, len(rejected), len(already),
    )
    if accepted and not line_comments:
        # Every finding landed at file level, so the quote-and-locate path
        # produced nothing usable. Correct, but it reads as a pile.
        log.warning(
            "%s/%s#%s: NOTHING anchored to a line. Check the 'line numbers' "
            "tally above for why.", owner, repo, pr,
        )


async def handle_dispute(job: asyncpg.Record) -> None:
    """A human replied to one of our inline comments. Answer them.

    The root comment IS the finding — file, line and text — so there is nothing
    to look up and no table to consult.
    """
    owner, repo, pr = job["owner"], job["repo"], job["pr_number"]
    inst = job["installation_id"]
    reply_id = job["trigger_comment_id"]

    # Fetch before reacting: a deleted comment gives a 403 on the way to a 404.
    try:
        reply = await client.get_review_comment(inst, owner, repo, reply_id)
    except client.GitHubError as exc:
        if "404" in str(exc):
            # Withdrawn before we got to it. Not a failure worth three retries.
            log.info("dispute %s: the comment was deleted, nothing to answer", reply_id)
            return
        raise

    with contextlib.suppress(client.GitHubError):
        await client.react_to_review_comment(inst, owner, repo, reply_id)

    root_id = reply.get("in_reply_to_id")
    if not root_id:
        log.info("dispute %s: not a reply, ignoring", reply_id)
        return

    root = await client.get_review_comment(inst, owner, repo, root_id)
    if (root.get("user") or {}).get("login") != config.BOT_LOGIN:
        # Someone replying to another human in a thread we never opened.
        log.info("dispute %s: root comment is not ours, ignoring", root_id)
        return

    objection = (reply.get("body") or "").strip()
    if not objection:
        return

    pull = await client.get_pull(inst, owner, repo, pr)
    head_sha = pull["head"]["sha"]
    token = await auth.installation_token(inst)

    async with checkout(owner, repo, head_sha, token) as repo_root:
        verdict = await recheck(
            repo_root=repo_root,
            tools=repo_tools(repo_root),
            file_path=root["path"],
            line=root.get("line") or root.get("original_line"),
            finding_body=root.get("body") or "",
            objection=objection,
        )

    conceded = verdict.outcome == "concede"
    prefix = "**Withdrawn.**" if conceded else "**Still stands.**"
    if config.DRY_RUN:
        log.info("[dry run] would reply on %s: %s %s",
                 root["path"], prefix, verdict.reasoning)
        return
    await client.reply_to_review_comment(
        inst, owner, repo, pr, root_id, f"{prefix} {verdict.reasoning}"
    )

    if conceded:
        # The concession made durable: the thread collapses and a later run can
        # see it was settled. REST cannot do this — the only reason for GraphQL.
        try:
            thread = await client.find_review_thread(inst, owner, repo, pr, root_id)
            if thread and not thread["isResolved"]:
                await client.resolve_review_thread(inst, thread["id"])
        except client.GitHubError as exc:
            # The reply is posted, so they have their answer. An open thread
            # is untidy, not wrong.
            log.warning("could not resolve thread for comment %s: %s", root_id, exc)

    log.info(
        "dispute %s/%s#%s by @%s on %s: %s",
        owner, repo, pr, job["requested_by"], root["path"], verdict.outcome,
    )


def _print_dry_run(
    owner: str, repo: str, pr: int,
    body: str, line_comments: list[dict], file_comments: list[dict],
    unplaceable: list[Any],
) -> None:
    """Show the review instead of posting it.

    Printed in full: the wording is the product, and a count says nothing about
    whether it is worth reading.
    """
    rule = "─" * 72
    print(f"\n{rule}\n  DRY RUN — nothing was posted to {owner}/{repo}#{pr}\n{rule}")
    print(f"\n[summary comment]\n{body}")
    for kind, comments in (("on a line", line_comments), ("file-level", file_comments)):
        for comment in comments:
            where = comment.get("path", "?")
            if comment.get("line"):
                where += f":{comment['line']}"
            print(f"\n[{kind}] {where}\n{comment.get('body', '')}")
    for finding in unplaceable:
        print(f"\n[no line found] {finding.file_path} — {finding.title}")
    print(
        f"\n{rule}\n  {len(line_comments)} on a line · {len(file_comments)} file-level · "
        f"{len(unplaceable)} unplaceable\n{rule}\n"
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
        # Not a failure: another worker has this PR, so requeue without spending
        # an attempt. The ceiling matters — a lock left by a killed worker can
        # have 40 minutes of TTL, and this logged once a second until noticed.
        count = await db.defer_job(job["id"], f"deferred: {exc}")
        if count >= config.MAX_JOB_DEFERRALS:
            log.warning(
                "%s deferred %d times and gave up — the lock is probably stale: %s",
                label, count, exc,
            )
            await db.finish_job(
                job["id"], error=f"gave up after {count} deferrals: {exc}"
            )
            return
        log.info("%s deferred (%d): %s", label, count, exc)
        await asyncio.sleep(config.DEFER_BACKOFF_SECONDS)
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
    # Switching profiles changes every finding, so it must never be a guess
    # when comparing two runs.
    log.info("model backend — %s", reviewer_config.llm_summary())
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
