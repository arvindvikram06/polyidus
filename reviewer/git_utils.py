from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except NotADirectoryError as exc:
        raise GitError(f"not a directory: {cwd}") from exc
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def ensure_git_repo(repo_root: Path) -> None:
    """Fail early, and legibly, when the target is not a git work tree.

    Outside a work tree `git diff` silently switches to `--no-index` mode, where
    `--staged` does not exist — so the underlying error is a confusing
    "unknown option `staged'" followed by the whole --no-index usage block.
    """
    if not repo_root.exists():
        raise GitError(f"path does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise GitError(f"not a directory: {repo_root}")

    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise GitError(
            f"not a git repository: {repo_root}\n"
            "This reviewer reads staged changes, so it needs a git work tree. "
            "Run `git init && git add .` there, or point --path at a repository."
        )


def get_staged_diff(repo_root: Path) -> str:
    return _run_git(["diff", "--staged", "--", ".", ":(exclude).reviewer/*", ":(exclude)*.lock", ":(exclude)package-lock.json"], repo_root)


def get_staged_files(repo_root: Path) -> list[str]:
    output = _run_git(["diff", "--staged", "--name-only", "--", ".", ":(exclude).reviewer/*", ":(exclude)*.lock", ":(exclude)package-lock.json"], repo_root)
    return [line for line in output.splitlines() if line]
