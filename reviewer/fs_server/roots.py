"""Which repositories this server is allowed to expose.

Over HTTP the repository path arrives from a *client* on a socket, not from a
human typing a CLI flag. ``tools.py`` confines readers inside a repo root,
but nothing there constrains the root itself — so the set of roots is pinned
here, at startup, and every client-supplied path is checked against it before it
reaches the filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path


class RootError(Exception):
    """A requested repository is outside the configured allowlist."""


class RootRegistry:
    def __init__(self, roots: list[Path]) -> None:
        if not roots:
            raise ValueError("at least one allowed root is required")
        resolved = [r.expanduser().resolve() for r in roots]
        for root in resolved:
            if not root.is_dir():
                raise ValueError(f"allowed root is not a directory: {root}")
        self._roots = resolved

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    @property
    def default(self) -> Path:
        return self._roots[0]

    def describe(self) -> str:
        return ", ".join(str(r) for r in self._roots)

    def resolve(self, repo: str | None) -> Path:
        """Map a client-supplied repo path onto an allowed root.

        With a single configured root ``repo`` may be omitted — the common case
        of "this server serves this project".
        """
        if not repo:
            if len(self._roots) > 1:
                raise RootError(
                    "this server exposes several roots; pass 'repo' explicitly. "
                    f"Allowed: {self.describe()}"
                )
            return self.default

        candidate = Path(repo).expanduser()
        if not candidate.is_absolute():
            candidate = self.default / candidate
        candidate = candidate.resolve()

        for root in self._roots:
            if candidate == root or candidate.is_relative_to(root):
                return candidate
        raise RootError(f"path is outside every allowed root: {repo}. Allowed: {self.describe()}")


def registry_from(argv_roots: list[str] | None = None) -> RootRegistry:
    """Build the registry from explicit paths, falling back to the environment."""
    raw = list(argv_roots or [])
    if not raw:
        raw = [p for p in os.environ.get("REVIEWER_ALLOWED_ROOTS", "").split(os.pathsep) if p]
    if not raw:
        raise ValueError(
            "no allowed roots configured. Pass paths as arguments, "
            "or set REVIEWER_ALLOWED_ROOTS."
        )
    return RootRegistry([Path(p) for p in raw])
