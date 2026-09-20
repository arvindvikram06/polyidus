"""The GitHub REST calls the bot makes, as the bot.

Only the handful it needs. Each takes an installation id and mints (or reuses)
a token for it, so a caller never handles credentials directly.

Note which endpoints are used for what, because GitHub's naming is a trap: a
comment in a pull request's conversation box is an *issue* comment, because
pull requests are issues in GitHub's data model. A comment on a line of code is
a *review* comment and lives inside a review. The two have different endpoints,
different reaction endpoints, and different webhook events.
"""

from __future__ import annotations

from typing import Any

import httpx

from bot import config
from bot.github import auth


class GitHubError(RuntimeError):
    pass


async def _request(
    installation_id: int,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    retry_auth: bool = True,
) -> Any:
    token = await auth.installation_token(installation_id)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            f"{config.GITHUB_API}{path}",
            json=json,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # A 401 on a token we thought was good means the installation changed
    # underneath us. Drop the cache and try once with a fresh one.
    if response.status_code == 401 and retry_auth:
        auth.forget(installation_id)
        return await _request(installation_id, method, path, json=json, retry_auth=False)

    if response.status_code >= 400:
        raise GitHubError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
    if not response.content:
        return None
    return response.json()


# ------------------------------------------------------------- reactions ----
async def react_to_issue_comment(
    installation_id: int, owner: str, repo: str, comment_id: int, content: str = "eyes"
) -> None:
    """Acknowledge instantly, without notifying anyone.

    A reaction sends no email and adds no row to the conversation, which is why
    it beats posting "working on it!" — that would notify every watcher, twice
    per review.
    """
    await _request(
        installation_id,
        "POST",
        f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
        json={"content": content},
    )


async def react_to_review_comment(
    installation_id: int, owner: str, repo: str, comment_id: int, content: str = "eyes"
) -> None:
    """Same thing for an inline comment — a different endpoint, note."""
    await _request(
        installation_id,
        "POST",
        f"/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions",
        json={"content": content},
    )


# -------------------------------------------------------------- comments ----
async def post_issue_comment(
    installation_id: int, owner: str, repo: str, pr_number: int, body: str
) -> dict[str, Any]:
    """A comment in the conversation box. Used for the review summary."""
    return await _request(
        installation_id,
        "POST",
        f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
        json={"body": body},
    )


async def reply_to_review_comment(
    installation_id: int, owner: str, repo: str, pr_number: int, comment_id: int, body: str
) -> dict[str, Any]:
    """Reply inside an existing inline thread. The dispute loop's output."""
    return await _request(
        installation_id,
        "POST",
        f"/repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
        json={"body": body},
    )


# ------------------------------------------------------------ pull request --
async def get_pull(installation_id: int, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    return await _request(installation_id, "GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")


async def get_pull_diff(installation_id: int, owner: str, repo: str, pr_number: int) -> str:
    """The unified diff, via the `.diff` media type.

    One request for the whole change, rather than paginating `/files` — and it
    cannot disagree with itself the way two calls can.
    """
    token = await auth.installation_token(installation_id)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"{config.GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.diff",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if response.status_code >= 400:
        raise GitHubError(f"get diff -> {response.status_code}: {response.text[:300]}")
    return response.text
