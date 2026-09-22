"""Put the pull request's code on disk, so specialists can actually read it.

Measured against the GitHub API: three specialists made 18 file-content calls,
opened one file between them, and every `search_code` returned nothing —
`search_code` has no `ref`, so it searches the default branch as last indexed
and can never see the code under review. A checkout makes grep a filesystem
walk over exactly the commit being reviewed.

**Clones to read, never to execute** — no build, no tests, no hooks. The
checkout lives for one review; it is a cache, never state.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("bot.workspace")


class WorkspaceError(RuntimeError):
    """The pull request could not be checked out."""


async def _git(*args: str, cwd: Path | None = None, timeout: float = 180) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # `git` must never prompt. Without this a repository it cannot read
        # hangs waiting for a username until the job times out.
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise WorkspaceError(f"git {args[0]} timed out after {timeout}s") from exc

    if process.returncode != 0:
        # The token appears in the remote URL, so never echo the command.
        message = stderr.decode(errors="replace").strip()
        raise WorkspaceError(f"git {args[0]} failed: {_redact(message)[:400]}")
    return stdout.decode(errors="replace")


def _redact(text: str) -> str:
    """Strip anything that looks like a token out of git's output."""
    import re

    return re.sub(r"(x-access-token:)[^@]+(@)", r"\1***\2", text)


@asynccontextmanager
async def checkout(owner: str, repo: str, head_sha: str, token: str):
    """Yield a directory containing ``head_sha``, and remove it afterwards.

    Fetched by SHA at depth 1: one commit, no history, no other branches — the
    fastest option and the narrowest, since the tree cannot hold a version of a
    file other than the one under review.
    """
    root = Path(tempfile.mkdtemp(prefix=f"reviewer-{repo}-"))
    try:
        # Token in the URL, not a header, so it never reaches a config file
        # or the reflog. The directory is removed below.
        url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

        await _git("init", "--quiet", str(root))
        await _git("remote", "add", "origin", url, cwd=root)
        await _git("fetch", "--quiet", "--depth", "1", "origin", head_sha, cwd=root)
        await _git("checkout", "--quiet", "FETCH_HEAD", cwd=root)

        # A silently wrong commit means reviewing different code from the diff.
        actual = (await _git("rev-parse", "HEAD", cwd=root)).strip()
        if actual != head_sha:
            raise WorkspaceError(f"checked out {actual[:8]}, expected {head_sha[:8]}")

        # Remove the remote, and with it the token, before any review tool runs.
        await _git("remote", "remove", "origin", cwd=root)

        files = sum(1 for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
        log.info("checked out %s/%s at %s into %s (%d files)",
                 owner, repo, head_sha[:8], root, files)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
