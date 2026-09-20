"""Repository and pull-request tools, served by GitHub's hosted MCP server.

Two connections, deliberately:

``read_tools()`` opens the ``/readonly`` toolset URLs and is what specialists
receive. They are driven by an untrusted diff, so the guarantee that they cannot
write has to be structural — here it is enforced by GitHub, which simply does
not expose a write tool on those paths. Filtering a combined tool list in our
own code would leave the guarantee one bug away from failing.

``review_tools()`` opens the writable pull-requests toolset and is used only by
the publishing step, which runs after a human approves and has no model in the
loop.
"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_core._api import suppress_langchain_beta_warning
from langchain_core.tools import BaseTool

with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from legacy.reviewer_cli.mcp.ratelimit import READ_POINTS, WRITE_POINTS, PointBucket
from legacy.reviewer_cli.mcp.tracing import as_text, traced
from reviewer.config import (
    GITHUB_MCP_BASE,
    GITHUB_MCP_POINTS_PER_MINUTE,
    GITHUB_READ_TOOL_ALLOWLIST,
    GITHUB_READ_TOOLSETS,
    GITHUB_REQUIRED_TOOL_ARGS,
    GITHUB_TOOL_DESCRIPTIONS,
    GITHUB_WRITE_TOOLSET,
)

# One bucket for the whole process: concurrent specialists share GitHub's
# per-minute budget whether they know about each other or not.
_BUCKET = PointBucket(points_per_minute=GITHUB_MCP_POINTS_PER_MINUTE)


class GitHubMcpError(Exception):
    """The GitHub MCP server could not be reached or exposed no tools."""


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise GitHubMcpError(
            "no GitHub token found. Set GITHUB_TOKEN to a token with `repo` scope "
            "(contents: read, pull_requests: read & write)."
        )
    return token


def _client(
    toolset: str,
    owner: str | None = None,
    repo: str | None = None,
    token: str | None = None,
) -> Client:
    """A FastMCP client bound to one toolset path, and to one repository.

    The server cross-checks `Mcp-Param-owner`/`Mcp-Param-repo` against the
    arguments of every call: supplying one without the other is rejected. That
    makes the connection itself repo-scoped, so a specialist driven by an
    untrusted diff cannot reach a repository we did not open the socket for.

    `token` lets a caller supply its own credential. The CLI passes nothing and
    gets the `GITHUB_TOKEN` PAT; the bot passes a GitHub App *installation*
    token, which this server accepts and which is scoped to the repositories
    the App is installed on — a narrower credential than a personal token, and
    one that expires on its own.
    """
    headers = {"Authorization": f"Bearer {token or github_token()}"}
    if owner:
        headers["Mcp-Param-owner"] = owner
    if repo:
        headers["Mcp-Param-repo"] = repo
    return Client(
        StreamableHttpTransport(f"{GITHUB_MCP_BASE.rstrip('/')}/x/{toolset}", headers=headers)
    )


def repo_overrides(
    tool: BaseTool, owner: str, repo: str, ref: str | None = None
) -> dict[str, str]:
    """Arguments this tool takes that the caller, not the model, must decide.

    `owner`/`repo`: the server rejects a call whose arguments disagree with the
    pinned headers, so leaving these to the model turns any drift — a
    hallucinated owner, a forgotten argument — into a hard error. Pinning them
    also stops the repository being something a prompt-injected specialist can
    choose.

    `ref`: without it every read resolves to the default branch, so a specialist
    inspecting a changed file silently gets the version *before* the pull
    request. Pinning it to the PR head is the difference between reviewing the
    change and reviewing what it replaced.
    """
    schema = tool.args_schema
    properties: dict = {}
    if hasattr(schema, "model_json_schema"):
        properties = schema.model_json_schema().get("properties") or {}
    elif isinstance(schema, dict):
        properties = schema.get("properties") or {}

    return {
        key: value
        for key, value in (("owner", owner), ("repo", repo), ("ref", ref))
        if key in properties and value
    }


async def _discover(
    toolset: str,
    owner: str | None = None,
    repo: str | None = None,
    token: str | None = None,
) -> list[BaseTool]:
    try:
        return await MCPAdapter(_client(toolset, owner, repo, token)).list_tools()
    except Exception as exc:
        raise GitHubMcpError(
            f"cannot reach the GitHub MCP toolset '{toolset}' at {GITHUB_MCP_BASE}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _explain_empty_search(name: str, result: Any) -> str | None:
    """Turn an unindexed-repository search into a statement the model can use.

    GitHub answers a search of a repository it has not indexed yet with
    `total_count: 0` and `incomplete_results: true`. To a model that reads as
    "no matches", so it rephrases the query and tries again — the distinction
    between "searched, found nothing" and "could not search" is invisible in the
    payload but decides whether retrying is worth anything.
    """
    if name != "search_code":
        return None
    text = as_text(result)
    if '"total_count":0' not in text.replace(" ", ""):
        return None
    if '"incomplete_results":true' not in text.replace(" ", ""):
        return None
    return (
        "note: this repository is not in GitHub's code search index yet "
        "(incomplete_results=true), which is normal for recently pushed code. "
        "Searching it will keep returning nothing however the query is phrased — "
        "read files directly with get_file_contents instead."
    )


async def _readonly_tools(
    owner: str,
    repo: str,
    ref: str | None,
    allowlist: tuple[str, ...] | None,
    token: str | None = None,
) -> list[BaseTool]:
    """Every read-only tool, optionally narrowed, pinned to one repo and commit."""
    tools: list[BaseTool] = []
    seen: set[str] = set()

    for toolset in GITHUB_READ_TOOLSETS:
        for tool in await _discover(toolset, owner, repo, token):
            # The readonly toolsets overlap; the first definition wins.
            if tool.name in seen:
                continue
            if allowlist and tool.name not in allowlist:
                continue
            seen.add(tool.name)
            override_description = GITHUB_TOOL_DESCRIPTIONS.get(tool.name)
            tools.append(
                traced(
                    tool,
                    before=lambda: _BUCKET.take(READ_POINTS),
                    overrides=repo_overrides(tool, owner, repo, ref),
                    required=GITHUB_REQUIRED_TOOL_ARGS.get(tool.name, ()),
                    annotate=_explain_empty_search,
                    description=(
                        override_description.format(owner=owner, repo=repo)
                        if override_description
                        else None
                    ),
                )
            )

    if not tools:
        raise GitHubMcpError(
            f"GitHub MCP exposed no usable tools across {list(GITHUB_READ_TOOLSETS)}"
            + (f" (wanted {list(allowlist)})" if allowlist else "")
        )
    return tools


async def read_tools(
    owner: str, repo: str, ref: str | None = None, token: str | None = None
) -> list[BaseTool]:
    """The narrow tool set handed to specialists.

    Narrow on purpose. The server offers sixteen read tools and a diff reviewer
    needs two; the rest — branches, collaborators, tags, releases — cannot answer
    a question about this change, but each is a plausible detour that costs an
    iteration. Specialists were exhausting their budget on them and returning no
    findings at all.
    """
    return await _readonly_tools(owner, repo, ref, GITHUB_READ_TOOL_ALLOWLIST, token)


async def pr_tools(owner: str, repo: str) -> list[BaseTool]:
    """The full read-only set, for the reviewer's own plumbing.

    Fetching the diff and listing pull requests is our code calling known tools,
    not a model choosing among them, so the allowlist that keeps specialists
    focused does not apply — and must not, since it excludes the very tool the
    diff is fetched with.
    """
    return await _readonly_tools(owner, repo, None, None)


async def review_tools(owner: str, repo: str) -> dict[str, BaseTool]:
    """Writable pull-request tools, keyed by name, for the publishing step."""
    tools = {
        tool.name: traced(
            tool,
            before=lambda: _BUCKET.take(WRITE_POINTS),
            overrides=repo_overrides(tool, owner, repo),
        )
        for tool in await _discover(GITHUB_WRITE_TOOLSET, owner, repo)
    }
    if not tools:
        raise GitHubMcpError(
            f"GitHub MCP exposed no tools for '{GITHUB_WRITE_TOOLSET}'"
        )
    return tools


async def call_tool(tools: dict[str, BaseTool], name: str, **arguments):
    """Invoke one MCP tool by name with a legible error when it is missing."""
    tool = tools.get(name)
    if tool is None:
        raise GitHubMcpError(
            f"GitHub MCP does not expose '{name}' (available: {', '.join(sorted(tools))})"
        )
    return await tool.ainvoke({k: v for k, v in arguments.items() if v is not None})


# Failures arrive as ordinary result text, not exceptions — but so do some
# successes ("pending pull request created"). So error detection has to match
# the failure shapes rather than assume anything non-JSON went wrong: requiring
# JSON rejected real successes, and requiring nothing reported six comments
# posted when every request had been refused.
_ERROR_MARKERS = (
    "resource not accessible",
    "not accessible by",
    "bad credentials",
    "requires authentication",
    "must have admin rights",
    "validation failed",
)


def looks_like_error(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    if lowered.startswith(("failed to ", "error:", "error ")):
        return True
    return any(marker in lowered for marker in _ERROR_MARKERS)


async def call_tool_checked(tools: dict[str, BaseTool], name: str, **arguments):
    """Invoke a tool and raise when its result reports a failure.

    Decoded JSON is returned when the result is JSON; otherwise the plain text
    is returned as-is, because several write tools answer with a sentence rather
    than a document. Any call whose outcome matters must come through here
    rather than `call_tool`.
    """
    raw = await call_tool(tools, name, **arguments)
    text = as_text(raw).strip()
    if looks_like_error(text):
        raise GitHubMcpError(f"{name} failed: {text[:400]}")
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
