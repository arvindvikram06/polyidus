"""Put the pull request's code on disk, so specialists can actually read it.

Why this exists, in one measurement: reviewing through GitHub's API, three
specialists made 18 `get_file_contents` calls and opened **one** file between
them, and all four `search_code` calls returned nothing. Every finding came
from the diff text alone.

Two limits of the API caused that, and neither is fixable with prompting:

* `search_code` has no `ref` parameter. It searches the repository's default
  branch, from whenever GitHub last indexed it — so it can never search the
  code in the pull request, which is the code under review.
* Reading is one network round trip per file, against a rate limit, with no
  way to grep.

With a checkout, `grep` is a filesystem walk over exactly the commit being
reviewed. It works on a repository pushed ten seconds ago.

**This clones to read, never to execute.** Nothing here runs anything from the
repository — no build, no tests, no hooks — so none of the risk of executing an
untrusted contributor's code applies. `git checkout` does not run repository
code, and the fetch is pinned to one commit.

The checkout's lifetime is the review. It is a cache, never state: deleting it
at any moment is safe, and re-creating it is one fetch.
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

    Fetched by SHA at depth 1 rather than cloned: one commit, no history, no
    other branches. That is both the fastest option and the narrowest — the
    working tree cannot contain a commit other than the one under review, so a
    specialist cannot accidentally read the wrong version of a file.
    """
    root = Path(tempfile.mkdtemp(prefix=f"reviewer-{repo}-"))
    try:
        # The token goes in the remote URL rather than a header so it never
        # reaches a config file or the reflog. The directory is removed below.
        url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

        await _git("init", "--quiet", str(root))
        await _git("remote", "add", "origin", url, cwd=root)
        await _git("fetch", "--quiet", "--depth", "1", "origin", head_sha, cwd=root)
        await _git("checkout", "--quiet", "FETCH_HEAD", cwd=root)

        # Prove we have what we think we have. A silently wrong commit would
        # mean reviewing different code from the diff, with no visible error.
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
