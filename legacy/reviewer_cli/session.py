"""Persist a review so a human can come back to it.

Sessions used to be write-only debug artifacts. Once a person has to triage
findings after the fact, the session is the handoff between the two halves of
the workflow, so it is keyed by target — one directory per pull request — and
readable.

The diff is stored once, at the top level, rather than inside every finding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from legacy.reviewer_cli.sources.base import ReviewTarget

if TYPE_CHECKING:
    from legacy.reviewer_cli.orchestrator import ReviewReport


def sessions_root(repo_root: Path) -> Path:
    return repo_root / ".reviewer" / "sessions"


def session_dir(repo_root: Path, target: ReviewTarget) -> Path:
    directory = sessions_root(repo_root) / target.slug
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_session(
    report: ReviewReport, repo_root: Path, target: ReviewTarget | None = None
) -> Path:
    target = target or report.target or ReviewTarget(kind="staged")
    directory = session_dir(repo_root, target)

    diff_text = report.diff_text
    if diff_text:
        (directory / "diff.patch").write_text(diff_text)

    # Written without any diff body. `Finding.diff_context` holds a full copy
    # of that run's diff on every single finding and is read nowhere, so a
    # review with 60 findings used to serialise 60 copies of the same patch.
    payload = report.model_dump(
        mode="json",
        exclude={"diff_text": True, "findings": {"__all__": {"diff_context"}}},
    )
    path = directory / "review.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_session(repo_root: Path, target: ReviewTarget) -> ReviewReport | None:
    from legacy.reviewer_cli.orchestrator import ReviewReport

    directory = sessions_root(repo_root) / target.slug
    path = directory / "review.json"
    if not path.is_file():
        return None

    raw = json.loads(path.read_text())
    patch = directory / "diff.patch"
    diff_text = patch.read_text() if patch.is_file() else ""

    # Findings were saved without their diff body; give them the review's diff
    # back. It is the whole patch rather than the per-run slice each finding
    # originally carried, which nothing downstream distinguishes.
    for finding in raw.get("findings", []):
        finding.setdefault("diff_context", diff_text)

    report = ReviewReport.model_validate(raw)
    report.diff_text = diff_text
    return report


def list_sessions(repo_root: Path) -> list[str]:
    root = sessions_root(repo_root)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "review.json").is_file())
