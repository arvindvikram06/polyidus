"""Where a review's diff comes from.

The master/specialist engine only ever sees a ``DiffContext``, so the input is
swappable: staged local changes during development, a pull request in anger.
``ReviewTarget`` carries the extra identity a PR review needs and a staged
review does not — which is also what decides whether findings can be posted.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from reviewer.models.diff_context import DiffContext


class ReviewTarget(BaseModel):
    """Identity of the thing under review."""

    kind: str  # "staged" | "pull_request"
    owner: str | None = None
    repo: str | None = None
    number: int | None = None
    title: str = ""
    author: str = ""
    # The commit the diff was computed against. Findings carry line numbers
    # valid only for this SHA; if the PR moves, a stored review is stale and
    # must not be posted.
    head_sha: str | None = None
    base_ref: str | None = None
    url: str = ""

    @property
    def slug(self) -> str:
        """Stable, filesystem-safe key for this target's session directory."""
        if self.kind == "pull_request":
            return f"{self.owner}-{self.repo}-pr{self.number}"
        return "staged"

    @property
    def is_postable(self) -> bool:
        return self.kind == "pull_request" and bool(self.owner and self.repo and self.number)


class ReviewSource(Protocol):
    async def load(self) -> tuple[DiffContext, ReviewTarget]: ...
