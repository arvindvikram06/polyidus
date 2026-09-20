"""The repository tools a specialist gets when reviewing from a checkout.

Background: reviewing through GitHub's API, three specialists made 18
`get_file_contents` calls and opened one file between them, and all four
`search_code` calls returned `total_count: 0` — because GitHub's code search
has no `ref` parameter and therefore cannot see a pull request's own code.

These tests use a temporary directory rather than a real checkout, so they run
without network. `bot.workspace.checkout` is what puts real code there.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.review.local_tools import repo_tools


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "Domain").mkdir(parents=True)
    (tmp_path / "src" / "Domain" / "Product.cs").write_text(
        "namespace Domain;\n"
        "\n"
        "public class Product\n"
        "{\n"
        "    public int Id { get; set; }\n"
        "    public string Sku { get; set; }\n"
        "}\n"
    )
    (tmp_path / "src" / "Repo.cs").write_text(
        'var sql = "WHERE SupplierCode = \'" + supplierCode + "\'";\n'
    )
    # Must never be readable: a prompt-injected diff asking for credentials
    # has to fail at the tool boundary, not at the model's discretion.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote]\n  url = https://token@github.com/x/y\n")
    (tmp_path / ".env").write_text("SECRET=hunter2\n")
    return tmp_path


def tools(repo: Path) -> dict:
    return {t.name: t for t in repo_tools(repo)}


def call(tool, **kwargs) -> str:
    return asyncio.run(tool.ainvoke(kwargs))


# --- the three tools exist and work -----------------------------------------


def test_a_specialist_gets_exactly_three_read_only_tools(repo: Path):
    names = sorted(tools(repo))
    assert names == ["list_directory", "read_file", "search_code"]


def test_search_finds_a_symbol_the_diff_does_not_define(repo: Path):
    """The capability the API could not provide at any price.

    `search_code` on GitHub searches the default branch from whenever it was
    last indexed, so a symbol added by the pull request is invisible to it.
    This searches the files actually checked out.
    """
    out = call(tools(repo)["search_code"], pattern="SupplierCode")
    assert "src/Repo.cs:1:" in out


def test_search_returning_nothing_means_it_is_not_there(repo: Path):
    """An empty result must be an answer, not a maybe.

    Through the API an empty result was ambiguous — missing, or merely
    unindexed — which is why a specialist reasonably kept rephrasing. Here it
    is definitive, and the prompt tells specialists to treat it that way.
    """
    assert call(tools(repo)["search_code"], pattern="SupplierCode") != "no matches"
    assert call(tools(repo)["search_code"], pattern="TotallyAbsentSymbol") == "no matches"


def test_reading_a_line_range_returns_only_those_lines(repo: Path):
    out = call(
        tools(repo)["read_file"], path="src/Domain/Product.cs", start_line=3, end_line=4
    )
    assert out.splitlines() == ["public class Product", "{"]


def test_listing_marks_directories(repo: Path):
    out = call(tools(repo)["list_directory"], path="src")
    assert "Domain/" in out
    assert "Repo.cs" in out


# --- the boundary -----------------------------------------------------------


def test_a_path_escaping_the_repository_is_refused(repo: Path):
    """The whole sandbox, in one assertion.

    With the API, "cannot read another repository" was GitHub's guarantee.
    From a checkout it is this check, so it is the thing worth testing.
    """
    out = call(tools(repo)["read_file"], path="../../../etc/passwd")
    assert out.startswith("error:")
    assert "escapes the repository root" in out


def test_git_and_env_are_not_readable(repo: Path):
    """`.git/config` holds the fetch URL, which carried an access token."""
    for path in (".git/config", ".env"):
        out = call(tools(repo)["read_file"], path=path)
        assert out.startswith("error:"), f"{path} was readable"
        assert "not readable by review tools" in out


def test_a_missing_file_answers_rather_than_raising(repo: Path):
    """A specialist that guessed wrong should try again, not have its run end."""
    out = call(tools(repo)["read_file"], path="src/Nope.cs")
    assert out.startswith("error:")
    assert "no such file" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
