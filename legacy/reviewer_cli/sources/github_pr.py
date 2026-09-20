"""Review a pull request entirely through GitHub's MCP server.

No clone, no fetch, no local working tree. The diff is the one GitHub itself
computes for the *Files changed* tab, which removes a whole class of error: we
are not reconstructing it from a base and a head, so we cannot reconstruct it
wrong, and the line numbers our findings anchor to are GitHub's own.
"""

from __future__ import annotations

import json
import re
from typing import Any

from legacy.reviewer_cli.mcp.github_client import GitHubMcpError, call_tool, pr_tools
from legacy.reviewer_cli.mcp.tracing import as_text
from legacy.reviewer_cli.sources.base import ReviewTarget
from reviewer.models.diff_context import DiffContext, split_by_file

_REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$")


def parse_remote(remote_url: str) -> tuple[str, str]:
    """Pull ``owner, repo`` out of an https or ssh GitHub remote."""
    match = _REMOTE.search(remote_url.strip())
    if not match:
        raise GitHubMcpError(f"not a recognisable GitHub remote: {remote_url}")
    return match.group("owner"), match.group("repo")


def _is_content_blocks(value: Any) -> bool:
    """True for an MCP content-block list, as opposed to a decoded JSON array.

    Both are lists, which is the trap: treating `[{"type": "text", "text": "[]"}]`
    as the payload yields one bogus element instead of an empty result, and for
    an object payload yields a list where a dict was expected — so every field
    silently reads as None.
    """
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(block, dict) and "text" in block for block in value)
    )


def _as_json(result: Any) -> Any:
    """MCP results arrive as text blocks; most carry JSON."""
    if _is_content_blocks(result):
        result = as_text(result)
    if isinstance(result, (dict, list)):
        return result
    text = str(result).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class GitHubPRSource:
    def __init__(self, owner: str, repo: str, number: int) -> None:
        self.owner = owner
        self.repo = repo
        self.number = number

    async def load(self) -> tuple[DiffContext, ReviewTarget]:
        tools = {tool.name: tool for tool in await pr_tools(self.owner, self.repo)}

        meta = _as_json(
            await call_tool(
                tools, "pull_request_read", method="get",
                owner=self.owner, repo=self.repo, pullNumber=self.number,
            )
        )
        diff_text = as_text(
            await call_tool(
                tools, "pull_request_read", method="get_diff",
                owner=self.owner, repo=self.repo, pullNumber=self.number,
            )
        )

        if not diff_text.strip():
            raise GitHubMcpError(
                f"{self.owner}/{self.repo}#{self.number} returned an empty diff"
            )

        # Derived from the diff rather than a second `get_files` call: one fewer
        # request, and it cannot disagree with the diff we actually review.
        changed_files = list(split_by_file(diff_text))

        target = ReviewTarget(
            kind="pull_request",
            owner=self.owner,
            repo=self.repo,
            number=self.number,
            title=_dig(meta, "title") or "",
            author=_dig(meta, "user", "login") or "unknown",
            # The commit the diff belongs to. Findings carry line numbers valid
            # only for this SHA, so it is what a stale-session check compares.
            head_sha=_dig(meta, "head", "sha") or "",
            base_ref=_dig(meta, "base", "ref") or "",
            url=_dig(meta, "html_url") or "",
        )
        return DiffContext(diff_text=diff_text, changed_files=changed_files), target


def _dig(payload: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


async def list_open_pulls(owner: str, repo: str) -> list[dict]:
    """Open pull requests, for the picker."""
    tools = {tool.name: tool for tool in await pr_tools(owner, repo)}
    result = _as_json(
        await call_tool(
            tools, "list_pull_requests", owner=owner, repo=repo, state="open", perPage=30
        )
    )
    if isinstance(result, dict):
        result = result.get("items") or result.get("pull_requests") or []
    return result if isinstance(result, list) else []
