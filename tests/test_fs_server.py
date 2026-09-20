from __future__ import annotations

import pytest
from mcp import Client

from legacy.reviewer_cli.mcp.fs_server_app import build_server
from legacy.reviewer_cli.mcp.roots import RootError, RootRegistry


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "db.py").write_text('q = f"SELECT * FROM users WHERE id={uid}"\nx = 1\n')
    (root / ".env").write_text("API_KEY=super-secret\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("url = git@github.com:acme/private.git\n")
    (tmp_path / "outside.txt").write_text("credentials\n")
    return root


@pytest.fixture
def client(repo):
    return lambda: Client(build_server(RootRegistry([repo])))


async def _call(c, name, args):
    result = await c.call_tool(name, args)
    return result.content[0].text if result.content else ""


# --- protocol surface ------------------------------------------------------


@pytest.mark.anyio
async def test_server_advertises_the_three_repo_tools(client):
    async with client() as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"grep_tool", "read_file_tool", "list_dir_tool"}


@pytest.mark.anyio
async def test_descriptions_carry_the_limits_agents_need(client):
    async with client() as c:
        by_name = {t.name: t.description or "" for t in (await c.list_tools()).tools}
    assert "50 matches" in by_name["grep_tool"]
    assert "2000 lines" in by_name["read_file_tool"]
    # no source indentation leaked in from a docstring
    assert not any(line.startswith("    ") for line in by_name["grep_tool"].splitlines())


@pytest.mark.anyio
async def test_tools_return_repo_content(client):
    async with client() as c:
        assert "app/db.py:1" in await _call(c, "grep_tool", {"pattern": "SELECT"})
        assert await _call(c, "read_file_tool", {"path": "app/db.py", "start_line": 2, "end_line": 2}) == "x = 1"
        assert "app/" in await _call(c, "list_dir_tool", {"path": "."})


# --- the sandbox must survive the protocol ---------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["../outside.txt", "app/../../outside.txt", "/etc/passwd"])
async def test_traversal_is_refused_over_the_wire(client, path):
    async with client() as c:
        assert "escapes the repository root" in await _call(c, "read_file_tool", {"path": path})


@pytest.mark.anyio
@pytest.mark.parametrize("path", [".env", ".git/config"])
async def test_denied_paths_are_refused_over_the_wire(client, path):
    async with client() as c:
        assert "not readable" in await _call(c, "read_file_tool", {"path": path})


@pytest.mark.anyio
async def test_grep_cannot_surface_denied_file_contents(client):
    async with client() as c:
        assert await _call(c, "grep_tool", {"pattern": "super-secret"}) == "no matches"
        assert await _call(c, "grep_tool", {"pattern": "acme/private"}) == "no matches"


@pytest.mark.anyio
async def test_denied_entries_are_hidden_from_listings(client):
    async with client() as c:
        listing = await _call(c, "list_dir_tool", {"path": "."})
    assert ".env" not in listing and ".git/" not in listing


@pytest.mark.anyio
async def test_repo_outside_the_allowlist_is_refused(client):
    """The root itself is client-supplied over HTTP, so it needs its own check."""
    async with client() as c:
        out = await _call(c, "read_file_tool", {"path": "passwd", "repo": "/etc"})
    assert "outside every allowed root" in out


@pytest.mark.anyio
async def test_a_bad_regex_is_reported_not_raised(client):
    async with client() as c:
        assert "invalid regex" in await _call(c, "grep_tool", {"pattern": "([unclosed"})


# --- the allowlist itself --------------------------------------------------


def test_single_root_may_be_omitted(repo):
    assert RootRegistry([repo]).resolve(None) == repo.resolve()


def test_several_roots_require_an_explicit_choice(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    registry = RootRegistry([repo, other])
    with pytest.raises(RootError, match="several roots"):
        registry.resolve(None)
    assert registry.resolve(str(other)) == other.resolve()


def test_relative_repo_resolves_under_the_default_root(repo):
    assert RootRegistry([repo]).resolve("app") == (repo / "app").resolve()


def test_a_root_that_is_not_a_directory_is_rejected(repo):
    with pytest.raises(ValueError, match="not a directory"):
        RootRegistry([repo / "app" / "db.py"])


# --- the server's own log --------------------------------------------------


@pytest.mark.anyio
async def test_successful_calls_are_logged_with_shape_not_content(client, caplog):
    with caplog.at_level("INFO", logger="reviewer.fs_server"):
        async with client() as c:
            await _call(c, "grep_tool", {"pattern": "SELECT"})
    record = next(r for r in caplog.records if "grep_tool" in r.message)
    assert record.levelname == "INFO"
    assert "pattern='SELECT'" in record.getMessage()
    assert "line(s)" in record.getMessage()
    # the log records how much came back, never the file contents themselves
    assert "super-secret" not in record.getMessage()


@pytest.mark.anyio
@pytest.mark.parametrize("path", [".env", "../outside.txt"])
async def test_refusals_are_logged_at_warning(client, caplog, path):
    """The sandbox is this server's purpose, so exercising it must be visible —
    the protocol reports a refusal as a perfectly successful call."""
    with caplog.at_level("INFO", logger="reviewer.fs_server"):
        async with client() as c:
            await _call(c, "read_file_tool", {"path": path})
    record = next(r for r in caplog.records if "read_file_tool" in r.message)
    assert record.levelname == "WARNING"
    assert "REFUSED" in record.getMessage()
