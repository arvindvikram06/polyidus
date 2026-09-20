from __future__ import annotations

import asyncio
import time

import pytest

from legacy.reviewer_cli.mcp import github_client as gh
from legacy.reviewer_cli.mcp.github_client import GitHubMcpError, call_tool
from legacy.reviewer_cli.mcp.ratelimit import READ_POINTS, WRITE_POINTS, PointBucket
from legacy.reviewer_cli.mcp.tracing import traced


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = name
        self.args_schema = None
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return f"{self.name}-result"


# --- the read/write split --------------------------------------------------


def test_specialists_only_ever_reach_readonly_toolset_urls(monkeypatch):
    """The guarantee that specialists cannot write is GitHub's, not ours.

    If this ever resolves to a writable path, a prompt-injected diff could post
    comments — so the URL is asserted, not the tool list.
    """
    urls: list[str] = []

    def fake_client(toolset):
        urls.append(toolset)
        return toolset

    async def fake_discover(toolset, owner=None, repo=None, token=None):
        fake_client(toolset)
        return [_Tool("get_file_contents"), _Tool("search_code")]

    monkeypatch.setattr(gh, "_discover", fake_discover)
    asyncio.run(gh.read_tools("acme", "widgets"))

    assert urls, "read_tools must open at least one toolset"
    assert all(u.endswith("/readonly") for u in urls), f"writable path reached: {urls}"


def test_the_publishing_connection_is_the_writable_toolset(monkeypatch):
    urls: list[str] = []

    async def fake_discover(toolset, owner=None, repo=None, token=None):
        urls.append(toolset)
        return [_Tool("pull_request_review_write")]

    monkeypatch.setattr(gh, "_discover", fake_discover)
    asyncio.run(gh.review_tools("acme", "widgets"))

    assert urls == ["pull_requests"]
    assert not urls[0].endswith("/readonly"), "posting needs write access"


def test_overlapping_readonly_toolsets_do_not_duplicate_tools(monkeypatch):
    async def fake_discover(toolset, owner=None, repo=None, token=None):
        return [_Tool("pull_request_read"), _Tool("search_code")]

    monkeypatch.setattr(gh, "_discover", fake_discover)
    tools = asyncio.run(gh.read_tools("acme", "widgets"))

    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "a duplicate tool confuses the model"


def test_a_missing_token_fails_before_any_network_call(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(GitHubMcpError, match="no GitHub token"):
        gh.github_token()


def test_the_toolset_url_is_built_from_the_configured_base(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    captured = {}

    class _Transport:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

    monkeypatch.setattr(gh, "StreamableHttpTransport", _Transport)
    monkeypatch.setattr(gh, "Client", lambda transport: transport)
    gh._client("repos/readonly")

    assert captured["url"].endswith("/x/repos/readonly")
    assert captured["headers"]["Authorization"] == "Bearer t"


def _capture_transport(monkeypatch) -> dict:
    """Intercept the transport so a connection's headers can be asserted."""
    captured: dict = {}

    class _Transport:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

    monkeypatch.setattr(gh, "StreamableHttpTransport", _Transport)
    monkeypatch.setattr(gh, "Client", lambda transport: transport)
    return captured


def test_a_caller_supplied_token_is_used_instead_of_the_environment(monkeypatch):
    """The bot authenticates as a GitHub App, not as a person.

    An App installation token is scoped to the repositories the App is installed
    on and expires on its own, so it is a strictly narrower credential than the
    PAT the CLI uses. If this ever silently fell back to the environment, the bot
    would read with someone's personal access instead of its own.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "personal-token")
    captured = _capture_transport(monkeypatch)

    gh._client("repos/readonly", "acme", "widgets", token="ghs_installation")

    assert captured["headers"]["Authorization"] == "Bearer ghs_installation"


def test_without_a_token_the_environment_pat_is_still_used(monkeypatch):
    """The CLI passes no token and must keep working unchanged."""
    monkeypatch.setenv("GITHUB_TOKEN", "personal-token")
    captured = _capture_transport(monkeypatch)

    gh._client("repos/readonly", "acme", "widgets")

    assert captured["headers"]["Authorization"] == "Bearer personal-token"


def test_a_supplied_token_reaches_the_specialists_tool_set(monkeypatch):
    """read_tools must thread the token all the way down to the connection.

    Asserted end to end rather than at `_client`, because the token passes
    through two intermediate functions and a missed argument at either would
    fall back to the environment without erroring.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "personal-token")
    seen: list[str | None] = []

    async def fake_discover(toolset, owner=None, repo=None, token=None):
        seen.append(token)
        return [_Tool("get_file_contents"), _Tool("search_code")]

    monkeypatch.setattr(gh, "_discover", fake_discover)
    asyncio.run(gh.read_tools("acme", "widgets", "deadbeef", token="ghs_installation"))

    assert seen, "read_tools opened no toolset"
    assert set(seen) == {"ghs_installation"}, f"token lost on the way down: {seen}"


def test_the_connection_is_pinned_to_one_repository_by_header(monkeypatch):
    """The server cross-checks Mcp-Param-* against each call's arguments.

    Sending one without the other is rejected, so the header is what stops a
    prompt-injected specialist reaching a repository we never opened.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    captured = {}

    class _Transport:
        def __init__(self, url, headers=None):
            captured["headers"] = headers

    monkeypatch.setattr(gh, "StreamableHttpTransport", _Transport)
    monkeypatch.setattr(gh, "Client", lambda transport: transport)
    gh._client("repos/readonly", "acme", "widgets")

    assert captured["headers"]["Mcp-Param-owner"] == "acme"
    assert captured["headers"]["Mcp-Param-repo"] == "widgets"


def _real_tool(name, fields):
    """A genuine StructuredTool — a Pydantic model, like the MCP adapter returns."""
    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    seen: list[dict] = []

    async def run(**kwargs):
        seen.append(kwargs)
        return "ok"

    schema = create_model(f"{name}Args", **{f: (str, ...) for f in fields})
    tool = StructuredTool(name=name, description=name, args_schema=schema, coroutine=run)
    return tool, seen


def test_owner_and_repo_are_forced_to_match_the_pinned_headers():
    """A model that guesses a different repo would get a hard server error."""
    tool, seen = _real_tool("get_file_contents", ["owner", "repo", "path"])
    bound = traced(tool, overrides=gh.repo_overrides(tool, "acme", "widgets"))

    asyncio.run(bound.ainvoke({"owner": "attacker", "repo": "evil", "path": "a.py"}))
    assert seen == [{"owner": "acme", "repo": "widgets", "path": "a.py"}]


def test_a_tool_without_repo_parameters_is_left_alone():
    tool, seen = _real_tool("search_repositories", ["query"])
    bound = traced(tool, overrides=gh.repo_overrides(tool, "acme", "widgets"))
    asyncio.run(bound.ainvoke({"query": "x"}))
    assert seen == [{"query": "x"}]


def test_calling_a_tool_the_server_does_not_expose_says_what_is_available():
    with pytest.raises(GitHubMcpError, match="available: a, b"):
        asyncio.run(call_tool({"a": _Tool("a"), "b": _Tool("b")}, "missing"))


def test_none_arguments_are_dropped_so_the_server_applies_defaults():
    tool = _Tool("add_comment_to_pending_review")
    asyncio.run(call_tool({tool.name: tool}, tool.name, path="a.py", line=3, startLine=None))
    assert tool.calls == [{"path": "a.py", "line": 3}]


# --- rate limiting ---------------------------------------------------------


def test_the_bucket_admits_calls_up_to_the_budget_without_delay():
    async def main():
        bucket = PointBucket(points_per_minute=10, window=60.0)
        started = time.monotonic()
        for _ in range(10):
            await bucket.take(READ_POINTS)
        return time.monotonic() - started

    assert asyncio.run(main()) < 0.1, "under budget must not throttle"


def test_the_bucket_throttles_once_the_window_is_full():
    async def main():
        bucket = PointBucket(points_per_minute=4, window=0.3)
        started = time.monotonic()
        for _ in range(6):
            await bucket.take(READ_POINTS)
        return time.monotonic() - started

    elapsed = asyncio.run(main())
    assert 0.25 < elapsed < 1.0, f"expected one window of delay, got {elapsed:.2f}s"


def test_writes_cost_five_times_a_read():
    """GitHub charges POST/PATCH/PUT/DELETE 5 points against the same budget."""

    async def main():
        bucket = PointBucket(points_per_minute=10, window=0.3)
        started = time.monotonic()
        for _ in range(2):
            await bucket.take(WRITE_POINTS)
        mid = time.monotonic() - started
        await bucket.take(WRITE_POINTS)  # 15 > 10, must wait
        return mid, time.monotonic() - started

    mid, total = asyncio.run(main())
    assert mid < 0.1, "first two writes fit in the budget"
    assert total > 0.25, "the third must wait for the window"


def test_concurrent_specialists_share_one_budget():
    """Four specialists reading at once are not four independent budgets."""

    async def main():
        bucket = PointBucket(points_per_minute=4, window=0.3)
        started = time.monotonic()
        await asyncio.gather(*(bucket.take(READ_POINTS) for _ in range(8)))
        return time.monotonic() - started

    assert asyncio.run(main()) > 0.25



# --- what the live run exposed ---------------------------------------------


def test_only_allowlisted_read_tools_reach_specialists(monkeypatch):
    """16 tools were offered; a diff reviewer needs two.

    Specialists spent their whole iteration budget on list_branches and
    list_repository_collaborators and never produced findings.
    """
    async def fake_discover(toolset, owner=None, repo=None, token=None):
        return [
            _Tool("get_file_contents"), _Tool("search_code"),
            _Tool("list_branches"), _Tool("list_repository_collaborators"),
            _Tool("list_releases"), _Tool("search_commits"),
        ]

    monkeypatch.setattr(gh, "_discover", fake_discover)
    names = {t.name for t in asyncio.run(gh.read_tools("acme", "widgets"))}
    assert names == {"get_file_contents", "search_code"}


def test_reads_are_pinned_to_the_reviewed_commit():
    """Unpinned, a file read returns the default branch — the version *before*
    the pull request. The specialist would review code the PR replaced."""
    tool, seen = _real_tool("get_file_contents", ["owner", "repo", "path", "ref"])
    bound = traced(tool, overrides=gh.repo_overrides(tool, "acme", "widgets", "deadbeef"))

    asyncio.run(bound.ainvoke({"owner": "x", "repo": "y", "path": "a.cs", "ref": "main"}))
    assert seen == [{"owner": "acme", "repo": "widgets", "path": "a.cs", "ref": "deadbeef"}]


def test_a_tool_without_a_ref_parameter_is_not_given_one():
    tool, seen = _real_tool("search_code", ["query"])
    bound = traced(tool, overrides=gh.repo_overrides(tool, "acme", "widgets", "deadbeef"))
    asyncio.run(bound.ainvoke({"query": "x"}))
    assert seen == [{"query": "x"}]


def test_repo_context_names_the_repository_and_demands_a_path():
    """`path` is optional on get_file_contents: omitted, it lists the root.

    A model that does not know the repository writes `repo:owner/name`
    literally into search queries and loops on directory listings.
    """
    from legacy.reviewer_cli.orchestrator import _repo_context
    from legacy.reviewer_cli.sources.base import ReviewTarget

    text = _repo_context(ReviewTarget(
        kind="pull_request", owner="acme", repo="widgets", number=1,
        head_sha="deadbeef", base_ref="main",
    ))
    assert "acme/widgets" in text
    assert "deadbeef" in text
    assert "MUST pass `path`" in text
    assert "repo:acme/widgets" in text

    assert _repo_context(ReviewTarget(kind="staged")) == ""


def test_specialists_are_told_the_repository_before_the_diff():
    """The context must precede the assignment, or it reads as an afterthought."""
    import inspect

    from reviewer.agents.subagents import base

    src = inspect.getsource(base.run_subagent_review)
    assert "repo_context" in src
    assert src.index("repo_context") < src.index("Your assignment for this run")


def test_plumbing_keeps_the_tools_the_allowlist_denies_specialists():
    """The source fetches the diff with pull_request_read, which specialists
    must not have. Filtering both through one list broke diff fetching."""
    async def fake_discover(toolset, owner=None, repo=None, token=None):
        return [_Tool("get_file_contents"), _Tool("search_code"),
                _Tool("pull_request_read"), _Tool("list_pull_requests")]

    import unittest.mock as m
    with m.patch.object(gh, "_discover", fake_discover):
        specialist = {t.name for t in asyncio.run(gh.read_tools("a", "b", "sha"))}
        plumbing = {t.name for t in asyncio.run(gh.pr_tools("a", "b"))}

    assert specialist == {"get_file_contents", "search_code"}
    assert {"pull_request_read", "list_pull_requests"} <= plumbing


# --- loop guards -----------------------------------------------------------


def test_omitting_a_required_arg_returns_a_correction_not_a_listing():
    """`get_file_contents` without `path` returns the repo root — a *successful*
    response, so the model never learns it asked wrongly and repeats forever."""
    tool, seen = _real_tool("get_file_contents", ["owner", "repo"])
    bound = traced(tool, required=("path",))

    result = asyncio.run(bound.ainvoke({"owner": "a", "repo": "b"}))
    assert "requires path" in str(result)
    assert seen == [], "the call must not reach the server at all"


def test_a_required_arg_that_is_present_passes_through():
    tool, seen = _real_tool("get_file_contents", ["owner", "repo", "path"])
    bound = traced(tool, required=("path",))
    asyncio.run(bound.ainvoke({"owner": "a", "repo": "b", "path": "x.cs"}))
    assert seen == [{"owner": "a", "repo": "b", "path": "x.cs"}]


def test_an_identical_repeat_call_is_answered_not_re_executed():
    """Two specialists made the same call 15 times and never produced findings."""
    tool, seen = _real_tool("get_file_contents", ["owner", "repo", "path"])
    bound = traced(tool)
    args = {"owner": "a", "repo": "b", "path": "x.cs"}

    first = asyncio.run(bound.ainvoke(dict(args)))
    second = asyncio.run(bound.ainvoke(dict(args)))

    assert first == "ok"
    assert "already called" in str(second)
    assert len(seen) == 1, "the repeat must not spend another API call"


def test_a_different_call_is_not_blocked_by_the_repeat_guard():
    tool, seen = _real_tool("get_file_contents", ["owner", "repo", "path"])
    bound = traced(tool)
    asyncio.run(bound.ainvoke({"owner": "a", "repo": "b", "path": "x.cs"}))
    asyncio.run(bound.ainvoke({"owner": "a", "repo": "b", "path": "y.cs"}))
    assert len(seen) == 2


def test_a_repeated_invalid_call_escalates_instead_of_looping():
    """The rejection used to return before anything was recorded, so the same
    invalid call looked new every time and a specialist repeated it eight times."""
    tool, seen = _real_tool("get_file_contents", ["owner", "repo"])
    bound = traced(tool, required=("path",))
    args = {"owner": "a", "repo": "b"}

    first = str(asyncio.run(bound.ainvoke(dict(args))))
    second = str(asyncio.run(bound.ainvoke(dict(args))))

    assert "requires path" in first and "twice" not in first
    assert "same invalid call twice" in second
    assert "report the findings you already have" in second
    assert seen == [], "neither call should reach the server"


def test_an_unindexed_repository_search_says_so_instead_of_looking_empty():
    """`total_count: 0` + `incomplete_results: true` means "could not search",
    not "no matches" — but both look identical to a model, which then keeps
    rephrasing a query that can never succeed."""
    unindexed = '{"total_count":0,"incomplete_results":true,"items":[]}'
    genuinely_empty = '{"total_count":0,"incomplete_results":false,"items":[]}'

    assert "not in GitHub's code search index" in (
        gh._explain_empty_search("search_code", unindexed) or ""
    )
    assert gh._explain_empty_search("search_code", genuinely_empty) is None
    assert gh._explain_empty_search("get_file_contents", unindexed) is None


def test_the_search_note_is_appended_to_the_real_result():
    tool, _ = _real_tool("search_code", ["query"])
    from langchain_core.tools import StructuredTool

    async def run(**kw):
        return '{"total_count":0,"incomplete_results":true,"items":[]}'

    tool = StructuredTool(
        name="search_code", description="s",
        args_schema=tool.args_schema, coroutine=run,
    )
    bound = traced(tool, annotate=gh._explain_empty_search)
    out = str(asyncio.run(bound.ainvoke({"query": "x"})))
    assert "total_count" in out and "code search index" in out


# --- required arguments are enforced in the schema, not just checked ---------


def _json_schema(tool):
    schema = tool.args_schema
    return schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema


def test_a_required_argument_is_promoted_to_required_in_the_schema():
    """GitHub declares `path` optional on `get_file_contents`.

    Called without it the server returns a directory listing — a *successful*
    response — so a model that omits it gets no signal it did anything wrong.

    Returning a correction as tool output was measured and does not work: on a
    real run, 17 of 18 calls were the identical no-path call, and one
    specialist made it nine more times after being told it had already made
    the same invalid call twice. Tool output is advice a model can decline.
    A required field is not advice — the call becomes unrepresentable.
    """
    from pydantic import BaseModel

    class _Args(BaseModel):
        owner: str
        repo: str
        path: str | None = None

    tool = _Tool("get_file_contents")
    tool.args_schema = _Args

    wrapped = traced(tool, required=("path",))

    assert "path" in _json_schema(wrapped)["required"]
    # The server's own requirements must survive.
    assert {"owner", "repo"} <= set(_json_schema(wrapped)["required"])


def test_promoting_an_argument_the_tool_does_not_have_is_ignored():
    """Demanding an argument the server does not accept would break every call."""
    from pydantic import BaseModel

    class _Args(BaseModel):
        query: str

    tool = _Tool("search_code")
    tool.args_schema = _Args

    wrapped = traced(tool, required=("path",))

    assert "path" not in _json_schema(wrapped).get("required", [])
    assert _json_schema(wrapped)["required"] == ["query"]


def test_the_tool_description_can_be_replaced_for_reviewers():
    """GitHub writes its descriptions for a general-purpose assistant.

    A specialist has one diff, a small budget, and no idea what files exist.
    The description is the only thing it reads before choosing a tool, so it
    is where the advice has to go — measured with the stock text, three
    specialists between them opened one file.
    """
    tool = _Tool("get_file_contents")
    assert tool.description == "get_file_contents"

    wrapped = traced(tool, description="Read a file. `path` is REQUIRED.")
    assert "REQUIRED" in wrapped.description

    # Without an override the server's own description survives.
    assert traced(_Tool("search_code")).description == "search_code"

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
