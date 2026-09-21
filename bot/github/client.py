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


async def get_review_comment(
    installation_id: int, owner: str, repo: str, comment_id: int
) -> dict[str, Any]:
    """One inline comment by id — used to fetch the root of a disputed thread.

    The root comment IS the finding: it carries the file, the line and the
    text the bot published. Nothing has to be looked up in a database, which
    is the whole reason findings are posted inline rather than listed in one
    summary comment.
    """
    return await _request(
        installation_id, "GET", f"/repos/{owner}/{repo}/pulls/comments/{comment_id}"
    )


# --------------------------------------------------------------- GraphQL ----
async def _graphql(installation_id: int, query: str, **variables: Any) -> dict[str, Any]:
    """The REST API cannot resolve a review thread. This is the only way.

    Resolution lives on ``PullRequestReviewThread.isResolved``, which REST does
    not expose at all — a review-comment object has no such field. So the one
    action that closes the human-in-the-loop cycle needs a second protocol.
    """
    token = await auth.installation_token(installation_id)
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            f"{config.GITHUB_API}/graphql",
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise GitHubError(f"graphql -> {response.status_code}: {response.text[:300]}")
    payload = response.json()
    if payload.get("errors"):
        # GraphQL reports failure in a 200 body. Without this the caller would
        # read `data: null` as an empty result rather than an error.
        raise GitHubError(f"graphql: {payload['errors']}")
    return payload["data"]


_THREAD_FOR_COMMENT = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes { id isResolved comments(first:1) { nodes { databaseId } } }
      }
    }
  }
}
"""


async def find_review_thread(
    installation_id: int, owner: str, repo: str, pr_number: int, root_comment_id: int
) -> dict[str, Any] | None:
    """The thread whose first comment is ``root_comment_id``.

    A thread's GraphQL id is not its REST comment id, so resolving a thread
    means finding it by the comment we already know about.
    """
    data = await _graphql(
        installation_id, _THREAD_FOR_COMMENT, owner=owner, repo=repo, number=pr_number
    )
    threads = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    for thread in threads:
        first = (thread["comments"]["nodes"] or [{}])[0]
        if first.get("databaseId") == root_comment_id:
            return thread
    return None


_RESOLVE = """
mutation($threadId:ID!) {
  resolveReviewThread(input:{threadId:$threadId}) {
    thread { id isResolved }
  }
}
"""


async def resolve_review_thread(installation_id: int, thread_id: str) -> None:
    """Mark a thread resolved — the bot conceding, in GitHub's own vocabulary."""
    await _graphql(installation_id, _RESOLVE, threadId=thread_id)


# --------------------------------------------------------------- reviews ----
async def create_review(
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    body: str,
    comments: list[dict[str, Any]],
    commit_id: str | None = None,
    event: str = "COMMENT",
) -> dict[str, Any]:
    """Post a review: one container, with every inline comment inside it.

    A review is the only way to attach several line comments as a single act.
    Posting them individually would send one notification per finding, which is
    how a reviewer bot becomes something people mute.

    ``event="COMMENT"`` publishes immediately. Omitting ``event`` would instead
    leave the review *pending* — a draft only the app itself can see, which the
    old CLI used as a human gate. The bot is invoked deliberately by a human
    comment, so it publishes.

    GitHub validates every comment's position against the diff and rejects the
    **whole** review with a 422 if any one of them is unplaceable. Callers
    should therefore be ready to fall back — see ``publish.post_review``.
    """
    payload: dict[str, Any] = {"body": body, "event": event, "comments": comments}
    if commit_id:
        # Pins the review to the commit the findings were computed against. If
        # the head has moved, GitHub rejects it rather than silently attaching
        # comments to lines that have shifted underneath.
        payload["commit_id"] = commit_id
    return await _request(
        installation_id, "POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", json=payload
    )


async def create_review_comment(
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    commit_id: str,
    path: str,
    body: str,
    line: int | None = None,
    side: str | None = None,
    start_line: int | None = None,
    start_side: str | None = None,
    subject_type: str | None = None,
) -> dict[str, Any]:
    """A single inline comment, outside any batched review.

    Only used as the fallback in ``publish.post_review``: this endpoint sends
    its own notification per call, which is exactly what the review container
    exists to avoid. Accepting one rejected comment beats losing the review.
    """
    payload: dict[str, Any] = {"commit_id": commit_id, "path": path, "body": body}
    for key, value in (
        ("line", line), ("side", side),
        ("start_line", start_line), ("start_side", start_side),
        ("subject_type", subject_type),
    ):
        if value is not None:
            payload[key] = value
    return await _request(
        installation_id, "POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/comments", json=payload
    )


async def list_review_comments(
    installation_id: int, owner: str, repo: str, pr_number: int
) -> list[dict[str, Any]]:
    """Every inline comment on the pull request, ours and everyone else's.

    This is the ledger. What the bot said last time is read back from here
    rather than from a database: the comment a human actually saw is the only
    record that cannot drift from what they were shown.
    """
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = await _request(
            installation_id,
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments?per_page=100&page={page}",
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


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
