"""Postgres access: the delivery ledger and the job queue.

Two things worth knowing about the design here.

``record_delivery`` relies on a unique-violation rather than a SELECT-then-
INSERT. GitHub's delivery IDs arrive concurrently when it retries, and a check
followed by an insert has a race between them; letting the primary key reject
the duplicate does not.

``claim_job`` uses ``FOR UPDATE SKIP LOCKED``, which is what makes the queue
safe for several workers. Each transaction locks the row it takes and skips
rows already locked, so two workers never claim the same job and neither waits
for the other.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from bot import config


class _State:
    """Holds the pool so the lazy initialiser needs no `global`."""

    pool: asyncpg.Pool | None = None


_state = _State()


async def pool() -> asyncpg.Pool:
    if _state.pool is None:
        _state.pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=1,
            max_size=10,
            # asyncpg returns JSONB as a string unless told otherwise; decoding
            # here keeps `json.loads` out of every call site.
            init=_register_json,
        )
    return _state.pool


async def _register_json(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def close() -> None:
    if _state.pool is not None:
        await _state.pool.close()
        _state.pool = None


# ------------------------------------------------------------- deliveries ---
async def record_delivery(delivery_id: str, event: str, action: str | None) -> bool:
    """Insert a delivery. False means we have seen it before — a replay.

    The caller must return 200 either way: telling GitHub a duplicate failed
    only makes it retry again.
    """
    async with (await pool()).acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO deliveries (delivery_id, event, action) VALUES ($1, $2, $3)",
                delivery_id,
                event,
                action,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


# -------------------------------------------------------------- the queue ---
async def enqueue(
    *,
    delivery_id: str,
    intent: str,
    owner: str,
    repo: str,
    pr_number: int,
    installation_id: int,
    requested_by: str,
    trigger_comment_id: int | None,
    payload: dict[str, Any],
) -> int:
    async with (await pool()).acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO jobs (delivery_id, intent, owner, repo, pr_number,
                              installation_id, requested_by, trigger_comment_id, payload)
            VALUES ($1, $2::job_intent, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            delivery_id,
            intent,
            owner,
            repo,
            pr_number,
            installation_id,
            requested_by,
            trigger_comment_id,
            payload,
        )


async def claim_job(worker_id: str) -> asyncpg.Record | None:
    """Take the oldest claimable job, or None.

    Claimable means queued, or claimed so long ago that the worker holding it
    is presumed dead. The whole thing is one statement so the select and the
    update cannot interleave with another worker.
    """
    async with (await pool()).acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id FROM jobs
             WHERE status = 'queued'
                OR (status = 'claimed'
                    AND claimed_at < now() - ($1::int * interval '1 second'))
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """,
            config.JOB_CLAIM_TIMEOUT_SECONDS,
        )
        if row is None:
            return None
        return await conn.fetchrow(
            """
            UPDATE jobs
               SET status = 'claimed', claimed_by = $2, claimed_at = now(),
                   attempts = attempts + 1
             WHERE id = $1
            RETURNING *
            """,
            row["id"],
            worker_id,
        )


async def finish_job(job_id: int, *, error: str | None = None) -> None:
    """Mark a job done, or failed. A job that has burned its attempts is dead.

    'dead' exists so a permanently failing job stops being retried forever —
    otherwise one malformed payload spins a worker for the life of the service.
    """
    async with (await pool()).acquire() as conn:
        if error is None:
            await conn.execute(
                "UPDATE jobs SET status='done', finished_at=now(), error=NULL WHERE id=$1",
                job_id,
            )
            return
        await conn.execute(
            """
            UPDATE jobs
               SET status = CASE WHEN attempts >= $3 THEN 'dead'::job_status
                                 ELSE 'queued'::job_status END,
                   error = $2,
                   claimed_by = NULL, claimed_at = NULL,
                   finished_at = CASE WHEN attempts >= $3 THEN now() ELSE NULL END
             WHERE id = $1
            """,
            job_id,
            error[:2000],
            config.MAX_JOB_ATTEMPTS,
        )


async def job_counts() -> dict[str, int]:
    """For the health endpoint and for eyeballing during a test run."""
    async with (await pool()).acquire() as conn:
        rows = await conn.fetch("SELECT status::text AS s, count(*) AS n FROM jobs GROUP BY 1")
    return {r["s"]: r["n"] for r in rows}
