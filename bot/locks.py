"""Per-pull-request mutex, in Redis.

Why this is not a Python variable: with one worker an in-process lock is
correct, and with two it silently stops working — each replica holds its own
copy, both believe they have the lock, and two reviews run concurrently on the
same PR posting duplicate comments. That bug does not appear in development.
It appears the first time you scale.

Different PRs run in parallel. The same PR serialises. Two pushes in quick
succession therefore queue rather than collide.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from bot import config


class _State:
    """Holds the client so the lazy initialiser needs no `global`."""

    client: aioredis.Redis | None = None


_state = _State()


def client() -> aioredis.Redis:
    if _state.client is None:
        _state.client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _state.client


async def close() -> None:
    if _state.client is not None:
        await _state.client.aclose()
        _state.client = None


def _key(owner: str, repo: str, pr_number: int) -> str:
    return f"prlock:{owner}/{repo}#{pr_number}"


class LockBusy(RuntimeError):
    """Another worker is already reviewing this pull request."""


@asynccontextmanager
async def pr_lock(owner: str, repo: str, pr_number: int):
    """Hold the lock for one PR, or raise LockBusy immediately.

    `SET key token NX EX ttl` is the acquire: atomic, and self-expiring so a
    worker that dies without releasing does not wedge the PR forever.

    The random token matters on release. A plain `DEL` would let a worker whose
    lock had already expired delete the lock a *different* worker has since
    taken — so the compare-and-delete is done in Lua, which Redis runs
    atomically.
    """
    key = _key(owner, repo, pr_number)
    token = str(uuid.uuid4())
    acquired = await client().set(key, token, nx=True, ex=config.PR_LOCK_TTL_SECONDS)
    if not acquired:
        holder = await client().get(key)
        raise LockBusy(f"{owner}/{repo}#{pr_number} is locked (held by {holder})")
    try:
        yield
    finally:
        await client().eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            key,
            token,
        )
