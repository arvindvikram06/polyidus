"""Today's behaviour, behind the source interface."""

from __future__ import annotations

from pathlib import Path

from legacy.reviewer_cli.sources.base import ReviewTarget
from legacy.reviewer_cli.utils.git_utils import ensure_git_repo, get_staged_diff, get_staged_files
from reviewer.models.diff_context import DiffContext


class StagedGitSource:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    async def load(self) -> tuple[DiffContext, ReviewTarget]:
        ensure_git_repo(self.repo_root)
        diff = get_staged_diff(self.repo_root)
        files = get_staged_files(self.repo_root)
        return (
            DiffContext(diff_text=diff, changed_files=files),
            ReviewTarget(kind="staged", title="staged changes"),
        )
