from __future__ import annotations

import os

import pytest

from reviewer.sandbox.files import ToolError, grep, list_dir, read_file


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


# --- ripgrep fast path ------------------------------------------------------


def _fake_rg(tmp_path, lines: list[str], exit_code: int = 0):
    """A stub `rg` on PATH, so the fast path is exercised without installing one.

    The output is written to a file and `cat`-ed rather than echoed, so the
    test's expectations are not hostage to shell escaping.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    payload = bindir / "rg-output.txt"
    payload.write_text("".join(f"{line}\n" for line in lines))
    script = bindir / "rg"
    script.write_text(f"#!/bin/sh\ncat {payload}\nexit {exit_code}\n")
    script.chmod(0o755)
    return bindir


def test_ripgrep_output_is_reformatted_to_the_same_contract(repo, tmp_path, monkeypatch):
    """`rg` prints `path:line:text`; callers expect `path:line: text`.

    The format is a contract — anchoring, the tracer and every caller parse it —
    so switching engines must not change it.
    """
    bindir = _fake_rg(tmp_path, ["./app/auth.py:1:SECRET = 1", "app/db.py:7:  SELECT *"])
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    out = grep(repo, "SECRET")

    assert out.splitlines() == ["app/auth.py:1: SECRET = 1", "app/db.py:7: SELECT *"]


def test_ripgrep_finding_nothing_is_no_matches_not_a_fallback(repo, tmp_path, monkeypatch):
    """Exit code 1 means "ran fine, found nothing" — not a failure.

    Treating it as one would silently re-run the slow path on every miss.
    """
    bindir = _fake_rg(tmp_path, [], exit_code=1)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    assert grep(repo, "AbsentSymbol") == "no matches"


def test_a_pattern_rust_rejects_is_reported_as_an_invalid_regex(repo, tmp_path, monkeypatch):
    """Rust's engine rejects some patterns Python accepts, such as lookarounds.

    That must surface as the error every caller already handles, not as a
    silent fallback that quietly searches for something else.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "rg"
    script.write_text(
        "#!/bin/sh\n"
        'echo "regex parse error: look-around is not supported" >&2\n'
        "exit 2\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    with pytest.raises(ToolError, match="invalid regex"):
        grep(repo, "(?=lookahead)")


def test_no_ripgrep_on_path_still_searches(repo, tmp_path, monkeypatch):
    """A slim container without `rg` must still review."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert "app/auth.py:1" in grep(repo, "SECRET")
