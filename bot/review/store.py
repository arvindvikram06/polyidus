"""Persisting a review: the run, its findings, and each specialist's notes.

Three of the seven tables get written here, and they have different lifetimes
on purpose (see docs/BOT_GUIDE.md §4):

``runs``        one row per invocation. Audit. Answers "why did it say that?"
``findings``    the PR ledger. One row per (PR, fingerprint). Load-bearing.
``scratchpads`` what each specialist did. Cheap, and useful when a human argues.

The insert into ``findings`` is ``ON CONFLICT DO NOTHING`` against the unique
constraint on ``(owner, repo, pr_number, fingerprint)``. That constraint is
doing real work already: two specialists who independently flag the same
problem produce one row, not two. Step 7 builds the cross-run dedupe on the
same key.
"""

from __future__ import annotations

import uuid
from typing import Any

from bot.db import pool
from bot.review.fingerprint import fingerprint as compute_fingerprint


async def open_run(
    *,
    job_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str | None,
) -> uuid.UUID:
    """Record that a review has started, before anything can fail.

    Written up front rather than at the end so a crashed review still leaves a
    row explaining what was attempted.
    """
    run_id = uuid.uuid4()
    async with (await pool()).acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runs (id, job_id, owner, repo, pr_number, head_sha, base_sha)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            run_id,
            job_id,
            owner,
            repo,
            pr_number,
            head_sha,
            base_sha,
        )
    return run_id


async def close_run(
    run_id: uuid.UUID,
    *,
    summary: str,
    routing: list[dict[str, Any]] | None = None,
    aborted: str | None = None,
) -> None:
    async with (await pool()).acquire() as conn:
        await conn.execute(
            """
            UPDATE runs
               SET summary = $2, routing = $3, aborted = $4, finished_at = now()
             WHERE id = $1
            """,
            run_id,
            summary,
            routing or [],
            aborted,
        )


async def save_findings(
    run_id: uuid.UUID,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    findings: list[Any],
    status: str | None = None,
) -> tuple[int, int]:
    """Write findings to the ledger. Returns ``(inserted, deduplicated)``.

    ``findings`` are ``reviewer.models.findings.Finding`` objects. They are
    mapped to columns here rather than persisted as JSON so the ledger can be
    queried — "what is still open on this PR", "which specialist gets
    overturned most" — without unpacking a blob.
    """
    inserted = 0
    rows = 0
    async with (await pool()).acquire() as conn:
        for f in findings:
            fp = compute_fingerprint(f.file_path, f.message, title=f.title)
            line_start, line_end = (f.line_range or (None, None))
            anchor_state = f.anchor.state.value if f.anchor else None
            # 'orphaned' when there is nowhere in the diff to attach it; the
            # review body carries those instead of dropping them silently.
            # A caller-supplied status wins — that is how failure records are
            # stored as 'suppressed' so they are diagnosable but never posted.
            row_status = status or ("orphaned" if anchor_state == "none" else "proposed")

            result = await conn.execute(
                """
                INSERT INTO findings (
                    id, run_id, owner, repo, pr_number, fingerprint,
                    subagent, verified_by, evidence, model_used,
                    file_path, line_start, line_end, hunk_header, offending_line, head_sha,
                    severity, title, message, suggested_patch,
                    anchor_state, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                        $21, $22::finding_status)
                ON CONFLICT (owner, repo, pr_number, fingerprint) DO NOTHING
                """,
                uuid.uuid4(),
                run_id,
                owner,
                repo,
                pr_number,
                fp,
                f.subagent,
                f.verified_by,
                [e.model_dump(mode="json") for e in (f.evidence or [])],
                f.model_used,
                f.file_path,
                line_start,
                line_end,
                f.hunk_header,
                f.offending_line,
                head_sha,
                f.severity.value,
                f.title,
                f.message,
                f.suggested_patch,
                anchor_state,
                row_status,
            )
            rows += 1
            # asyncpg returns the command tag; "INSERT 0 0" means the conflict
            # clause fired and nothing was written.
            if result.endswith(" 1"):
                inserted += 1
    return inserted, rows - inserted


async def save_scratchpads(run_id: uuid.UUID, trace: list[Any]) -> None:
    """One row per specialist run, derived from the master's trace.

    This is the *observational* version: what the specialist was asked, which
    files it was scoped to, how it finished. The richer version — where a
    specialist writes its own `ruled_out` and `open_questions` as it works, so
    it stops re-reading files it has already dismissed — needs a tool the
    specialist can call, and is not built yet.
    """
    async with (await pool()).acquire() as conn:
        for index, entry in enumerate(trace):
            await conn.execute(
                """
                INSERT INTO scratchpads (
                    run_id, agent, task_index, task, files_scoped, confirmed, open_questions
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (run_id, agent, task_index) DO NOTHING
                """,
                run_id,
                entry.subagent,
                index,
                entry.task,
                list(entry.files or []),
                [f"{entry.finding_count} finding(s)"],
                [entry.error] if entry.error else [],
            )


async def ledger(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Everything known about this pull request, newest run first.

    Not used by the review path yet — this is what step 7's dedupe reads, and
    what makes `psql` useful while testing.
    """
    async with (await pool()).acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT fingerprint, subagent, severity, title, file_path,
                   line_start, status::text AS status, anchor_state, created_at
              FROM findings
             WHERE owner = $1 AND repo = $2 AND pr_number = $3
             ORDER BY created_at DESC
            """,
            owner,
            repo,
            pr_number,
        )
    return [dict(r) for r in rows]
