from __future__ import annotations

import pytest

from reviewer.fs_server.tools import ToolError, grep, list_dir, read_file


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "auth.py").write_text("SECRET = os.environ['S']\ndef login():\n    pass\n")
    (tmp_path / ".env").write_text("API_KEY=super-secret\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url = git@github.com:acme/private.git\n")
    # A sibling of the repo, i.e. the thing traversal would be reaching for.
    (tmp_path.parent / "outside.txt").write_text("credentials\n")
    return tmp_path


def test_reads_a_file_inside_the_repo(repo):
    assert "def login():" in read_file(repo, "app/auth.py")


def test_line_range_is_respected(repo):
    assert read_file(repo, "app/auth.py", 2, 2) == "def login():"


@pytest.mark.parametrize(
    "path", ["../outside.txt", "app/../../outside.txt", "/etc/passwd", "app/../../"]
)
def test_traversal_out_of_the_repo_is_refused(repo, path):
    with pytest.raises(ToolError):
        read_file(repo, path)


@pytest.mark.parametrize("path", [".env", ".git/config"])
def test_sensitive_paths_are_refused_even_inside_the_repo(repo, path):
    with pytest.raises(ToolError, match="not readable"):
        read_file(repo, path)


def test_list_dir_hides_denied_entries(repo):
    entries = list_dir(repo, ".")
    assert "app/" in entries
    assert ".env" not in entries
    assert ".git/" not in entries


def test_list_dir_refuses_to_escape(repo):
    with pytest.raises(ToolError, match="escapes"):
        list_dir(repo, "..")


def test_grep_finds_repo_content(repo):
    assert "app/auth.py:1" in grep(repo, "SECRET")


def test_grep_never_reaches_denied_files(repo):
    # The secret is in .env and .git/config; neither may surface.
    assert grep(repo, "super-secret") == "no matches"
    assert grep(repo, "acme/private") == "no matches"


def test_grep_rejects_an_invalid_regex(repo):
    with pytest.raises(ToolError, match="invalid regex"):
        grep(repo, "([unclosed")


def test_grep_does_not_follow_symlinks_out_of_the_repo(repo):
    (repo / "leak.txt").symlink_to(repo.parent / "outside.txt")
    assert grep(repo, "credentials") == "no matches"
